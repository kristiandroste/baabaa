"""LAN-only guard: which addresses baabaa listens on, which clients it accepts, which Host names.

Two modes (the owner's setting "network", `baabaa network local|lan`):
- lan (the default for data folders from before the setting): listen on loopback and on the IPv4 address of
  the interface that carries the default route; accept clients from loopback and that interface's subnet.
- local: listen on loopback only and accept only this computer.
IPv6 is not served, because hosts often hold globally routable IPv6 addresses. Virtual bridges
(containers, VMs) are not trusted unless configured.
"""

import ipaddress
import re
import socket
import struct
import subprocess
import sys

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
MODES = ("local", "lan")


def _ioctl_addr(sock, name: str, request: int) -> str | None:
    import fcntl
    try:
        packed = fcntl.ioctl(sock.fileno(), request, struct.pack("256s", name[:15].encode()))
        return socket.inet_ntoa(packed[20:24])
    except OSError:
        return None


def interfaces() -> list[dict]:
    """IPv4 interfaces: [{'name', 'ip', 'netmask', 'network'}]."""
    if sys.platform == "darwin":
        return parse_ifconfig(_run(["/sbin/ifconfig"]))
    out = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for _, name in socket.if_nameindex():
            ip = _ioctl_addr(s, name, SIOCGIFADDR)
            mask = _ioctl_addr(s, name, SIOCGIFNETMASK)
            if ip and mask:
                net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
                out.append({"name": name, "ip": ip, "netmask": mask, "network": str(net)})
    return out


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_ifconfig(text: str) -> list[dict]:
    """macOS `ifconfig`: each interface's IPv4 address and netmask ("inet 192.168.1.20 netmask 0xffffff00")."""
    out, name = [], None
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9]+): flags=", line)
        if m:
            name = m.group(1)
            continue
        m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]{8})", line)
        if m and name:
            mask = socket.inet_ntoa(struct.pack("!I", int(m.group(2), 16)))
            net = ipaddress.IPv4Network(f"{m.group(1)}/{mask}", strict=False)
            out.append({"name": name, "ip": m.group(1), "netmask": mask, "network": str(net)})
    return out


def parse_route_get(text: str) -> str | None:
    """macOS `route -n get default`: the interface that carries the default route."""
    m = re.search(r"^\s*interface:\s*(\S+)", text, re.M)
    return m.group(1) if m else None


def default_interface() -> str | None:
    if sys.platform == "darwin":
        return parse_route_get(_run(["/sbin/route", "-n", "get", "default"]))
    try:
        with open("/proc/net/route") as f:
            next(f)
            best = None
            for line in f:
                fields = line.split()
                if len(fields) >= 8 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                    metric = int(fields[6])
                    if best is None or metric < best[1]:
                        best = (fields[0], metric)
            return best[0] if best else None
    except OSError:
        return None


def saved_mode(maindb) -> str:
    """The owner's choice: "local" (this computer only) or "lan". A new data folder starts as "local"; one from
    before the choice existed was given "lan" when it was first opened (maindb.py). Anything unreadable is "local"."""
    mode = maindb.get_setting("network")
    return mode if mode in MODES else "local"


class LanGuard:
    def __init__(self, bind: list[str] | None = None, networks: list[str] | None = None, names: list[str] | None = None,
                 local_only: bool = False):
        self.local_only = local_only
        ifaces = [] if local_only else interfaces()
        default = None if local_only else default_interface()
        primary = next((i for i in ifaces if i["name"] == default), None)
        self.bind = bind or (["127.0.0.1"] + ([primary["ip"]] if primary else []))
        nets = [] if local_only else (networks or ([primary["network"]] if primary else []))
        self.networks = [ipaddress.ip_network("127.0.0.0/8")] + [ipaddress.ip_network(n, strict=False) for n in nets]
        hostname = socket.gethostname().lower()
        self.hostname = hostname
        self.names = {"localhost", hostname, f"{hostname}.local", f"{hostname}.lan", f"{hostname}.home"}
        self.names.update(n.lower() for n in (names or []))
        self.addresses = {i["ip"] for i in ifaces} | set(self.bind)
        self.primary_ip = primary["ip"] if primary else "127.0.0.1"

    def allowed_client(self, ip: str) -> bool:
        if ip == "local":  # Unix socket
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if addr.version == 6:
            if addr.ipv4_mapped:
                addr = addr.ipv4_mapped
            else:
                return addr.is_loopback
        return any(addr in n for n in self.networks)

    def allowed_host(self, host_header: str | None) -> bool:
        if not host_header:
            return False
        host = host_header.strip().lower()
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        else:
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return host in self.names or host in self.addresses

    def allowed_origin(self, origin: str | None) -> bool:
        if not origin:
            return True  # same-origin requests from older browsers or the terminal client
        if origin == "null":
            return False
        try:
            scheme, rest = origin.split("://", 1)
        except ValueError:
            return False
        return scheme in ("http", "https") and self.allowed_host(rest.split("/", 1)[0])

    def urls(self, port: int, tls: bool) -> list[str]:
        scheme = "https" if tls else "http"
        lan = [a for a in self.bind if not a.startswith("127.")]
        out = [f"{scheme}://{a}:{port}/" for a in lan]
        if lan:
            out.append(f"{scheme}://{self.hostname}.local:{port}/")
        out.append(f"{scheme}://localhost:{port}/")
        return out
