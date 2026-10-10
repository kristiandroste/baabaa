"""Names on the local network, such as baabaa.local, with multicast DNS (mDNS, RFC 6762).

A phone or computer looks up a `.local` name by asking the whole network, and the device with that name
answers with its address. On the network (LAN mode), baabaa answers for baabaa.local and ai.local, and for any
`--name` that ends in .local, with the IPv4 address it serves the network on. As every device does, it first asks
three times whether another device has the name (probing), and takes the next number if one does
(baabaa-2.local); then it announces the name, answers queries for it, and says goodbye when it stops (TTL 0),
so devices forget it at once.

Linux only for now. The responder here shares port 5353 with the system's own (Avahi), but receives only
multicast, so unicast mDNS keeps reaching the system. On macOS only the system's responder may receive mDNS,
so baabaa leaves names to it there (the Mac's own name.local works). IPv4 only, like the rest of baabaa's
network serving (lan.py).
"""

import asyncio
import random
import re
import socket
import struct
import sys
import time

GROUP, PORT = "224.0.0.251", 5353
TTL = 120          # seconds; RFC 6762 recommends 120 for records that carry an address
LEGACY_TTL = 10    # for ordinary resolvers that ask from another port than 5353
A, AAAA, NSEC, ANY = 1, 28, 47, 255
IN = 1
TOP = 0x8000       # in a question's class: "answer by unicast"; in a record's class: "cache-flush" (unique record)
RESPONSE, AUTHORITATIVE, OPCODE = 0x8000, 0x0400, 0x7800
LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
DEFAULT = ["baabaa", "ai"]  # baabaa.local and ai.local
TRIES = 9          # baabaa.local, then baabaa-2.local … baabaa-9.local
SUPPORTED = sys.platform.startswith("linux")


def clean_names(values) -> list[str]:
    """Names as single labels ("Baabaa.local" -> "baabaa"); ValueError for one a network cannot carry."""
    out = []
    for value in values:
        name = str(value).strip().lower()
        name = name[:-6] if name.endswith(".local") else name
        if not LABEL.fullmatch(name):
            raise ValueError(f"{value!r} cannot be a name on the network: use letters, digits and hyphens")
        if name not in out:
            out.append(name)
    return out


# the wire format (RFC 1035) ---------------------------------------------------------------------------------------
def encode_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p for p in (label.encode() for label in name.split("."))) + b"\0"


def read_name(data: bytes, pos: int) -> tuple[str, int]:
    """The name at `pos`, lowercased, following compression pointers, and the position after it."""
    labels, end, jumps = [], None, 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated name")
        size = data[pos]
        if size & 0xC0 == 0xC0:
            if pos + 1 >= len(data) or jumps > 16:
                raise ValueError("bad pointer")
            end = pos + 2 if end is None else end
            pos, jumps = ((size & 0x3F) << 8) | data[pos + 1], jumps + 1
            continue
        if size & 0xC0 or pos + 1 + size > len(data):
            raise ValueError("bad label")
        if size == 0:
            return ".".join(labels), pos + 1 if end is None else end
        labels.append(data[pos + 1:pos + 1 + size].lower().decode("latin-1"))
        pos += 1 + size


def parse(data: bytes) -> dict:
    """A message's header, questions [(name, type, class)] and records [{section, name, type, class, ttl, data}]."""
    if len(data) < 12:
        raise ValueError("short message")
    ident, flags, qd, an, ns, ar = struct.unpack("!6H", data[:12])
    pos, questions, records = 12, [], []
    for _ in range(qd):
        name, pos = read_name(data, pos)
        if pos + 4 > len(data):
            raise ValueError("short question")
        questions.append((name, *struct.unpack("!2H", data[pos:pos + 4])))
        pos += 4
    for section, count in (("answer", an), ("authority", ns), ("additional", ar)):
        for _ in range(count):
            name, pos = read_name(data, pos)
            if pos + 10 > len(data):
                raise ValueError("short record")
            rtype, rclass, ttl, size = struct.unpack("!HHIH", data[pos:pos + 10])
            pos += 10
            if pos + size > len(data):
                raise ValueError("short record data")
            records.append({"section": section, "name": name, "type": rtype, "class": rclass, "ttl": ttl,
                            "data": data[pos:pos + size]})
            pos += size
    return {"id": ident, "flags": flags, "questions": questions, "records": records}


def record(name: str, rtype: int, rdata: bytes, ttl: int, flush: bool = True) -> bytes:
    return encode_name(name) + struct.pack("!HHIH", rtype, IN | (TOP if flush else 0), ttl, len(rdata)) + rdata


def nsec(name: str) -> bytes:
    """NSEC data saying the name has an A record and nothing else (RFC 6762 §6.1), so nobody waits for IPv6."""
    return encode_name(name) + b"\x00\x01\x40"


def message(answers=(), authority=(), additional=(), questions=(), ident=0, flags=RESPONSE | AUTHORITATIVE) -> bytes:
    head = struct.pack("!6H", ident, flags, len(questions), len(answers), len(authority), len(additional))
    asked = b"".join(encode_name(n) + struct.pack("!2H", t, c) for n, t, c in questions)
    return head + asked + b"".join(answers) + b"".join(authority) + b"".join(additional)


# the responder ----------------------------------------------------------------------------------------------------
class Responder(asyncio.DatagramProtocol):
    PROBE_GAP = 0.25     # RFC 6762 §8.1: three probes 250 ms apart
    ANNOUNCE_GAP = 1.0   # §8.3: two announcements a second apart

    def __init__(self, ip: str, accept=None, log=None):
        self.ip, self.addr = ip, socket.inet_aton(ip)
        self.accept = accept or (lambda host: True)
        self.log = log or (lambda text: None)
        self.names: list[str] = []          # claimed, e.g. "baabaa.local"
        self.probing: dict[str, bool] = {}  # a name being probed -> another device has it
        self.sent: dict[tuple, float] = {}  # (name, type) -> when it was last multicast
        self.transport = None
        self.tasks: set[asyncio.Task] = set()

    async def start(self, wanted: list[str]) -> list[str]:
        """Claims the names (["baabaa", "ai"] -> ["baabaa.local", "ai.local"]); [] when it cannot run here."""
        if not wanted or not SUPPORTED:
            return []
        try:
            sock = self._socket()
        except OSError as exc:
            self.log(f"names on the network are off: port {PORT} cannot be shared here ({exc.strerror or exc})")
            return []
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=sock)
        claimed = [n for n in await asyncio.gather(*(self._claim(w) for w in wanted)) if n]
        self.names = claimed
        if claimed:
            task = loop.create_task(self._announce())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return claimed

    def close(self) -> None:
        """Goodbye: TTL 0 tells every device to forget the names now."""
        for task in list(self.tasks):
            task.cancel()
        if self.transport is not None:
            if self.names:
                self._send(message(answers=[record(n, A, self.addr, 0) for n in self.names]))
            self.transport.close()
            self.transport = None

    def _socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # shared with the system's responder
            try:  # Linux: only the group joined here, on the network's interface (not containers' or VPNs')
                s.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_MULTICAST_ALL", 49), 0)
            except OSError:
                pass
            s.bind((GROUP, PORT))  # multicast only, so unicast mDNS to this computer still reaches the system
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP) + self.addr)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, self.addr)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            s.setblocking(False)
            return s
        except OSError:
            s.close()
            raise

    def _send(self, data: bytes, to=(GROUP, PORT)) -> None:
        if self.transport is not None:
            self.transport.sendto(data, to)

    # claiming a name ----------------------------------------------------------------------------------------------
    async def _claim(self, base: str) -> str | None:
        for n in range(1, TRIES + 1):
            name = f"{base}.local" if n == 1 else f"{base}-{n}.local"
            if name in self.probing or name in self.names:
                continue
            if await self._probe(name):
                self.names.append(name)
                if n > 1:
                    self.log(f"{base}.local is taken by another device on this network; baabaa is {name}")
                return name
        self.log(f"{base}.local is taken on this network, and so are {base}-2 to {base}-{TRIES}")
        return None

    async def _probe(self, name: str) -> bool:
        self.probing[name] = False
        try:
            await asyncio.sleep(random.uniform(0, self.PROBE_GAP))
            for _ in range(3):
                if self.probing[name]:
                    return False
                # multicast answers only (no unicast bit): this socket does not receive unicast
                self._send(message(questions=[(name, ANY, IN)], authority=[record(name, A, self.addr, TTL, flush=False)],
                                   flags=0))
                await asyncio.sleep(self.PROBE_GAP)
            return not self.probing[name]
        finally:
            del self.probing[name]

    async def _announce(self) -> None:
        for i in range(2):
            if i:
                await asyncio.sleep(self.ANNOUNCE_GAP)
            self._send(message(answers=[record(n, A, self.addr, TTL) for n in self.names],
                               additional=[record(n, NSEC, nsec(n), TTL) for n in self.names]))
            now = time.monotonic()
            self.sent.update({(n, A): now for n in self.names})

    def _ours(self, r: dict) -> bool:
        return r["type"] == A and r["data"] == self.addr

    # receiving ------------------------------------------------------------------------------------------------------
    def datagram_received(self, data, addr):
        if not self.accept(addr[0]):
            return
        try:
            msg = parse(data)
        except (ValueError, struct.error):
            return
        if msg["flags"] & OPCODE:
            return
        if msg["flags"] & RESPONSE:  # another device's answer: a conflict for a name being probed
            for r in msg["records"]:
                if r["name"] in self.probing and not self._ours(r):
                    self.probing[r["name"]] = True
            return
        for r in msg["records"]:  # another device probing for the same name: the later data wins (§8.2)
            if r["section"] == "authority" and r["name"] in self.probing and \
                    (r["class"] & ~TOP, r["type"], r["data"]) > (IN, A, self.addr):
                self.probing[r["name"]] = True
        self._answer(msg, addr)

    def _answer(self, msg: dict, addr) -> None:
        legacy = addr[1] != PORT  # an ordinary resolver: a unicast reply with its ID and question (§6.7)
        probe = any(r["section"] == "authority" for r in msg["records"])  # defended at once (§8.1)
        known = {r["name"] for r in msg["records"]
                 if r["section"] == "answer" and self._ours(r) and r["ttl"] >= TTL // 2}  # §7.1
        now, answers, extra, asked = time.monotonic(), [], [], []
        for name, qtype, qclass in msg["questions"]:
            if name not in self.names or (qclass & ~TOP) not in (IN, ANY):
                continue
            rtype = A if qtype in (A, ANY) else NSEC  # anything else: "this name has an A record only"
            if not legacy and ((rtype == A and name in known) or
                               (not probe and now - self.sent.get((name, rtype), -9.0) < 1.0)):  # §6: once a second
                continue
            ttl = LEGACY_TTL if legacy else TTL
            rec = record(name, rtype, self.addr if rtype == A else nsec(name), ttl, flush=not legacy)
            if rec not in answers:
                answers.append(rec)
                asked.append((name, qtype, qclass & ~TOP))
                if not legacy:
                    self.sent[(name, rtype)] = now
            if rtype == A and not legacy:
                more = record(name, NSEC, nsec(name), ttl)
                if more not in extra:
                    extra.append(more)
        if not answers:
            return
        if legacy:
            self._send(message(answers=answers, questions=asked, ident=msg["id"]), addr)
        else:
            self._send(message(answers=answers, additional=[e for e in extra if e not in answers]))

    def error_received(self, exc):
        pass
