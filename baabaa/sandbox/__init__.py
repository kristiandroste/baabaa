"""The tool sandbox: on Linux, Landlock (filesystem, TCP ports) and seccomp (sockets, dangerous syscalls); on
macOS, Seatbelt (seatbelt.py).

Tool processes start through `python3 -m baabaa.sandbox`, which confines itself (Linux) or hands the command
to `sandbox-exec` with a profile (macOS), so the confinement covers the command and everything it starts. If
the rules cannot be enforced, the helper exits with status 126 and the command never runs.
"""

import os
import sys

MAC = sys.platform == "darwin"

# Read-only system locations a normal command needs. Anything else (other users' homes, baabaa's own
# data, other accounts' files) stays invisible unless granted.
SYSTEM_RO = ["/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt", "/proc",
             "/sys", "/run", "/snap", "/nix", "/var/lib/dpkg", "/usr/local", "/dev"]
DEV_RW = ["/dev/null", "/dev/zero", "/dev/full", "/dev/tty", "/dev/ptmx", "/dev/pts", "/dev/urandom", "/dev/random"]
SAFE_ENV = ("PATH", "LANG", "LANGUAGE", "TZ", "TERM", "COLORTERM", "NO_COLOR", "FORCE_COLOR")
DEFAULT_PORTS = (80, 443)


HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__main__.py")


def helper_argv(argv: list[str], cwd: str, rw: list[str], ro: list[str], net: bool, ports=DEFAULT_PORTS) -> list[str]:
    """The command line that runs `argv` inside the sandbox (the helper runs by path, from any folder)."""
    cmd = [sys.executable, "-s", HELPER, "--cwd", cwd, "--net", "on" if net else "off"]
    for p in rw:
        cmd += ["--rw", p]
    for p in ro:
        cmd += ["--ro", p]
    if net:
        cmd += ["--ports", ",".join(str(p) for p in ports)]
    return cmd + ["--"] + argv


STANDARD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"


def sandbox_path(extra_roots: list[str] = ()) -> str:
    """The server's PATH, keeping only directories the sandbox can see (so a user's own Python or node
    under their home folder is not picked up and then refused)."""
    if MAC:  # everything outside the closed areas is readable, plus the given folders
        from .seatbelt import CLOSED, STANDARD_PATH as MAC_PATH
        closed = CLOSED + ("/tmp", "/var/folders")
        keep = []
        for d in os.environ.get("PATH", MAC_PATH).split(":") + MAC_PATH.split(":"):
            inside = any(d == r or d.startswith(r.rstrip("/") + "/") for r in extra_roots)
            if d and d not in keep and (inside or not any(d == c or d.startswith(c + "/") for c in closed)):
                keep.append(d)
        return ":".join(keep)
    roots = [r for r in SYSTEM_RO if r not in ("/proc", "/sys", "/dev", "/run", "/etc")] + list(extra_roots)
    keep = []
    for d in os.environ.get("PATH", STANDARD_PATH).split(":") + STANDARD_PATH.split(":"):
        if d and d not in keep and any(d == r or d.startswith(r.rstrip("/") + "/") for r in roots):
            keep.append(d)
    return ":".join(keep)


def sandbox_env(home: str, tmp: str, extra: dict | None = None, roots: list[str] = ()) -> dict:
    env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
    env["PATH"] = sandbox_path(roots)
    env.update({"HOME": home, "TMPDIR": tmp, "BAABAA_SANDBOX": "1", "PAGER": "cat", "GIT_PAGER": "cat",
                "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8" if MAC else "C.UTF-8")})
    if extra:
        env.update(extra)
    return env
