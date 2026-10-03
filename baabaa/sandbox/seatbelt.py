"""The tool sandbox on macOS: Seatbelt (`sandbox-exec`), with a profile written for each command.

It keeps Landlock's promises on Linux (sandbox/__init__.py): a command can change only the folders it is given
to write (its working folder, folders granted read-write, the account's own home and temporary folders); it
can read system locations and the folders it is given, but nobody's home folder, external volumes or shared
temporary folders otherwise; and it has no network unless allowed, then only TCP to the allowed ports (and
name lookups).

How Seatbelt decides: the last rule that matches an operation wins, so the profile closes areas first and
opens the given folders after (Bazel's macOS sandbox relies on the same order). Rules name real paths as the
kernel sees them: /tmp is /private/tmp, and on a case-insensitive disk the case must be the disk's, which
`canonical` asks the kernel for (F_GETPATH). Paths reach the profile as parameters (-D), never pasted into it.

Not yet run on a Mac (written 2026-10-02): `baabaa doctor --sandbox` checks every promise on the machine.
"""

import os
import sys

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
F_GETPATH = 50  # <sys/fcntl.h> on macOS
CLOSED = ("/Users", "/Volumes", "/private/tmp", "/private/var/folders")  # personal, removable and shared areas
DEV_WRITE = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/dtracehelper", "/dev/ptmx")
STANDARD_PATH = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin"


def canonical(path: str) -> str:
    """The path as the kernel names it: symbolic links resolved and, on a case-insensitive disk, the disk's case."""
    real = os.path.realpath(path)
    if sys.platform != "darwin":
        return real
    try:
        import fcntl
        fd = os.open(real, os.O_RDONLY)
    except (OSError, ImportError):
        return real
    try:
        buf = fcntl.fcntl(fd, F_GETPATH, b"\0" * 1024)
        name = buf.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
        return name or real
    except OSError:
        return real
    finally:
        os.close(fd)


def profile(rw: list[str], ro: list[str], net: bool, ports=(), resolve=canonical) -> tuple[str, dict]:
    """(profile text, parameters) for one command."""
    params: dict[str, str] = {}

    def sub(path: str) -> str:
        key = f"P{len(params)}"
        params[key] = resolve(path)
        return f'(subpath (param "{key}"))'

    lines = ["(version 1)", "(allow default)",
             f"(deny file-read-data file-read-xattr file-write* {' '.join(sub(c) for c in CLOSED)})",
             "(deny file-write*)"]
    writable = [sub(d) for d in rw if d]
    readable = [sub(d) for d in ro if d]
    if writable:
        lines.append(f"(allow file-read-data file-read-xattr file-write* {' '.join(writable)})")
    if readable:
        lines.append(f"(allow file-read-data file-read-xattr {' '.join(readable)})")
    lines.append("(allow file-write* " + " ".join(f'(literal "{d}")' for d in DEV_WRITE) + ' (regex #"^/dev/ttys[0-9]+$"))')
    lines.append("(deny network*)")
    if net:
        allowed = " ".join(f'(remote tcp "*:{int(p)}")' for p in ports)
        if allowed:
            lines.append(f"(allow network-outbound {allowed})")
        lines.append('(allow network-outbound (literal "/private/var/run/mDNSResponder"))')  # name lookups
    return "\n".join(lines) + "\n", params


def argv(cmd: list[str], rw: list[str], ro: list[str], net: bool, ports=()) -> list[str]:
    """The command line that runs `cmd` confined."""
    text, params = profile(rw, ro, net, ports)
    out = [SANDBOX_EXEC, "-p", text]
    for key, value in params.items():
        out += ["-D", f"{key}={value}"]
    return out + list(cmd)


def available() -> bool:
    return os.access(SANDBOX_EXEC, os.X_OK)
