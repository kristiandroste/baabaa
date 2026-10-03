"""What baabaa reads from the operating system that differs between Linux and macOS: the process table, the CPU
time processes have used, and system facts. Linux reads /proc; macOS asks `ps` and `sysctl`."""

import os
import re
import subprocess
import sys

MAC = sys.platform == "darwin"
_TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") and not MAC else 100


def _run(argv: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def processes() -> list[dict]:
    """Every process this user can see: [{'pid', 'ppid', 'argv'}], argv as a list of bytes. On macOS the
    command line comes from `ps` and is split at spaces (enough to tell programs apart)."""
    if MAC:
        return parse_ps(_run(["/bin/ps", "-A", "-o", "pid=,ppid=,command="]))
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            with open(f"/proc/{name}/cmdline", "rb") as f:
                argv = [a for a in f.read().split(b"\0") if a]
        except (OSError, IndexError, ValueError):
            continue
        out.append({"pid": int(name), "ppid": ppid, "argv": argv})
    return out


def parse_ps(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            argv = [w.encode() for w in (parts[2].split() if len(parts) > 2 else [])]
            out.append({"pid": int(parts[0]), "ppid": int(parts[1]), "argv": argv})
    return out


def children(pid: int) -> list[int]:
    return [p["pid"] for p in processes() if p["ppid"] == pid]


def cpu_seconds(pids) -> float | None:
    """CPU time (user + system, all threads) these processes have used so far; None if none can be read."""
    pids = [int(p) for p in pids or ()]
    if not pids:
        return None
    if MAC:
        text = _run(["/bin/ps", "-o", "time=", "-p", ",".join(str(p) for p in pids)])
        times = [parse_cputime(t) for t in text.split()]
        times = [t for t in times if t is not None]
        return sum(times) if times else None
    total, seen = 0.0, False
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            total += (int(fields[11]) + int(fields[12])) / _TICK  # utime and stime, fields 14 and 15
            seen = True
        except (OSError, IndexError, ValueError):
            continue
    return total if seen else None


def parse_cputime(text: str) -> float | None:
    """`ps -o time` on macOS: [[dd-]hh:]mm:ss[.ss]."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", text.strip())
    if not m:
        return None
    days, hours, minutes, seconds = m.groups()
    return int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def process_name(pid: int) -> str:
    if MAC:
        return os.path.basename(_run(["/bin/ps", "-o", "comm=", "-p", str(pid)]).strip()) or "?"
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return "?"


def sysctl(name: str) -> str | None:
    """A macOS sysctl value as text (None elsewhere, or when it does not exist)."""
    if not MAC:
        return None
    out = _run(["/usr/sbin/sysctl", "-n", name]).strip()
    return out or None
