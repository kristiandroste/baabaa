"""`baabaa start | stop | restart | status`: the server as a background process that keeps running after the
terminal it was started from is closed.

`start` runs `baabaa serve` (with the same options) in a session of its own, with no terminal, so closing
the window or ending an SSH session does not stop it. Its output goes to `<data>/logs/server.log` (the
previous run's log is kept as `server.log.1`), and `<data>/server.json` records how it was started.
`stop` asks the server to shut down, which stops its model servers and unloads its models first, and
waits for it. One server runs per data folder: `serve` holds a lock on `<data>/server.lock` while it runs,
so `status` and `stop` also find a server that was started in a terminal.
"""

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

READY_S = 90     # how long `start` waits for the server to listen
STOP_S = 60      # how long `stop` waits for a clean shutdown before it ends the server by force
REPO = str(Path(__file__).resolve().parent.parent)


def _files():
    from .paths import Paths
    paths = Paths()
    logs = paths.root / "logs"
    logs.mkdir(exist_ok=True, mode=0o700)
    return paths, paths.root / "server.json", logs / "server.log"


# the lock that `serve` holds -----------------------------------------------------------------------
def hold_lock(root: Path):
    """Called by `serve`: take the data folder's lock for the life of the process, or exit when another
    server holds it. The lock goes away with the process, however it ends."""
    f = open(root / "server.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        other = f.read().strip() or "?"
        print(f"baabaa is already running for {root} (pid {other}). `baabaa status` shows it; `baabaa stop` stops it.")
        sys.exit(1)
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f  # keep it open: closing it releases the lock


def running_pid(root: Path) -> int | None:
    """The pid of the server that holds `root`'s lock, or None when no server runs for it."""
    try:
        f = open(root / "server.lock", "a+")
    except OSError:
        return None
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # held: a server is running
            f.seek(0)
            text = f.read().strip()
            return int(text) if text.isdigit() else -1
        fcntl.flock(f, fcntl.LOCK_UN)
        return None


# asking the running server ---------------------------------------------------------------------------
def ask(sock_path: Path, path: str = "/api/health", timeout: float = 3.0) -> dict | None:
    """GET `path` over the server's local socket; None if it does not answer."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock_path))
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
            data = b""
            while chunk := s.recv(65536):
                data += chunk
    except OSError:
        return None
    head, _, body = data.partition(b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n", 1)[0] + b" ":
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def post(sock_path: Path, path: str, body: dict, headers: dict | None = None, timeout: float = 5.0) -> dict | None:
    """POST JSON to `path` over the server's local socket; None if it does not answer with 200."""
    payload = json.dumps(body).encode()
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock_path))
            s.sendall(f"POST {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {len(payload)}\r\n{extra}\r\n".encode() + payload)
            data = b""
            while chunk := s.recv(65536):
                data += chunk
    except OSError:
        return None
    head, _, raw = data.partition(b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n", 1)[0] + b" ":
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _startup_lines(log: Path) -> list[str]:
    """The lines `serve` prints once it listens: its addresses, the CA, the first-run link."""
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return []
    for i, line in enumerate(lines):
        if line.startswith("baabaa") and " is running " in line:  # "baabaa 1.2.3 is running on ..."
            out = [line]
            for more in lines[i + 1:]:
                if not more.startswith(" "):
                    break
                out.append(more)
            return out
    return []


def _tail(log: Path, n: int = 12) -> str:
    try:
        return "\n".join(log.read_text(errors="replace").strip().splitlines()[-n:])
    except OSError:
        return ""


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return f"{int(seconds)} s"
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if h < 48 else f"{h // 24} days"


def _systemd_main_pid() -> int | None:
    from .installer import service_main_pid
    return service_main_pid()


def _systemd(action: str, paths, quiet: bool = False) -> int:
    """start, stop or restart through the service that starts baabaa with the computer (systemd or launchd)."""
    from .installer import service, service_log
    code, message = service(action)
    if code != 0:
        print(f"Could not {action} baabaa through the service manager: {message}\nIts log: {service_log()}")
        return 1
    if action == "stop":
        t0 = time.monotonic()
        while time.monotonic() - t0 < STOP_S and running_pid(paths.root):
            time.sleep(0.3)
        if not quiet:
            print("baabaa stopped.")
        return 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < READY_S:
        health = ask(paths.socket)
        if health and health.get("urls"):
            print(f"baabaa is running: {health['urls'][0]}   (it starts with the computer; log: {service_log()})")
            return 0
        time.sleep(0.5)
    print(f"baabaa is still starting; `baabaa status` shows when it is ready. Log: {service_log()}")
    return 0


# the commands ------------------------------------------------------------------------------------------
def start(serve_args: list[str], quiet: bool = False) -> int:
    paths, state_file, log = _files()
    pid = running_pid(paths.root)
    if pid:
        print(f"baabaa is already running (pid {pid}).")
        for line in _startup_lines(log):
            print(line)
        return 0
    from .installer import managed
    if managed():  # baabaa starts with the computer: systemd runs it
        return _systemd("start", paths)
    if log.exists():
        log.replace(log.with_name("server.log.1"))
    env = dict(os.environ)
    env["BAABAA_HOME"] = str(paths.root)
    env["PYTHONPATH"] = REPO + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"  # the log shows each line as it happens
    with open(log, "ab") as out:
        from . import BOOT
        proc = subprocess.Popen([sys.executable, "-c", BOOT, REPO, "serve", *serve_args], cwd=str(paths.root), env=env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    state = {"pid": proc.pid, "args": serve_args, "started": time.time(), "python": sys.executable}
    fd = os.open(state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    t0 = time.monotonic()
    while time.monotonic() - t0 < READY_S:
        if proc.poll() is not None:
            state_file.unlink(missing_ok=True)
            print(f"baabaa did not start:\n{_tail(log)}")
            return 1
        lines = _startup_lines(log)
        if lines:
            if not quiet:
                for line in lines:
                    print(line)
                print(f"It keeps running after this terminal closes. `baabaa status` shows it; `baabaa stop` stops it.\n"
                      f"Log: {log}")
            return 0
        time.sleep(0.3)
    print(f"baabaa is still starting (pid {proc.pid}); `baabaa status` shows when it is ready. Log: {log}")
    return 0


def _children(pid: int) -> list[int]:
    from .system import children
    return children(pid)


def stop(quiet: bool = False) -> int:
    paths, state_file, log = _files()
    pid = running_pid(paths.root)
    if not pid:
        if not quiet:
            print("baabaa is not running.")
        state_file.unlink(missing_ok=True)
        return 0
    from .installer import managed
    if managed() and _systemd_main_pid() == pid:
        return _systemd("stop", paths, quiet)
    if pid < 0:
        print(f"A baabaa server holds {paths.root / 'server.lock'} but its pid is unknown; stop it where it runs.")
        return 1
    kids = _children(pid)  # its model servers, in case it must be ended by force
    try:
        os.kill(pid, signal.SIGTERM)  # the server stops its model servers and unloads its models
    except ProcessLookupError:
        pass
    t0 = time.monotonic()
    while time.monotonic() - t0 < STOP_S and running_pid(paths.root):
        time.sleep(0.3)
    if running_pid(paths.root):
        for p in [pid] + kids:
            try:
                if p != pid and os.getpgid(p) == p:
                    os.killpg(p, signal.SIGKILL)  # a model server, in its own group with anything it started
                else:
                    os.kill(p, signal.SIGKILL)  # never a group here: a server started in a terminal shares the shell's
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.5)
        print(f"baabaa did not stop within {STOP_S} s, so it was ended (pid {pid}).")
    elif not quiet:
        print(f"baabaa stopped (pid {pid}, {time.monotonic() - t0:.0f} s).")
    state_file.unlink(missing_ok=True)
    return 0


def restart() -> int:
    paths, state_file, log = _files()
    from .installer import managed
    if managed():
        return _systemd("restart", paths)
    try:
        args = json.loads(state_file.read_text()).get("args") or []
    except (OSError, ValueError):
        args = []
    stop(quiet=True)
    return start(args)


def _update_lines(health: dict) -> list[str]:
    """What `status` says about updates and a waiting restart (from the server's own view)."""
    out = []
    pending = health.get("restart")
    if pending:
        out.append(f"  Restarting {'now' if pending.get('now') else 'when the replies it is writing are finished'}"
                   f" (asked by {pending.get('by')}).")
    u = health.get("update") or {}
    target, running = u.get("target"), health.get("version")
    if u.get("state") == "available":
        out.append(f"  Update: baabaa {target} is available. Install it with: baabaa update")
    elif u.get("state") == "ready":
        out.append(f"  Update: baabaa {target} is downloaded and checked. Install it with: baabaa update")
    elif u.get("state") == "installed":
        out.append(f"  Update: baabaa {target} is installed, but the server still runs {running}. Restart it: baabaa restart")
    elif u.get("state") == "changed":
        out.append("  baabaa's files changed since the server started. Restart it to use them: baabaa restart")
    return out


def status() -> int:
    """Exit status 0 when the server runs, 3 when it does not (as service managers do)."""
    paths, state_file, log = _files()
    pid = running_pid(paths.root)
    if not pid:
        print("baabaa is not running. Start it with: baabaa start")
        if state_file.exists():  # started with `baabaa start`, and not stopped with `baabaa stop`
            tail = _tail(log, 6)
            print(f"It ended on its own; the end of its log ({log}):\n{tail}" if tail else "")
            state_file.unlink(missing_ok=True)
        return 3
    try:
        state = json.loads(state_file.read_text())
        if state.get("pid") != pid:
            state = {}
    except (OSError, ValueError):
        state = {}
    since = state.get("started")
    from .installer import managed
    started_how = ("by the service manager (it starts with the computer)" if managed() and _systemd_main_pid() == pid
                   else "in the background" if state else "in a terminal")
    health = ask(paths.socket)
    version = f"baabaa {health['version']}" if health and health.get("version") else "baabaa"
    print(f"{version} is running {started_how} (pid {pid}" + (f", for {_ago(time.time() - since)})" if since else ")"))
    if health is None:
        print("  It is not answering yet (still starting, or busy).")
    else:
        for line in _update_lines(health):
            print(line)
        urls = health.get("urls") or []
        if urls:
            print(f"  Open: {urls[0]}" + (f"   (also {', '.join(urls[1:])})" if urls[1:] else ""))
        if health.get("network") == "local":
            print("  It accepts this computer only (baabaa network lan lets other devices in).")
        elif health.get("addresses"):
            print(f"  On a phone or computer on your network, type {' or '.join(health['addresses'])}")
        q = health.get("queue") or {}
        paused = q.get("paused")
        if paused:
            print(f"  GPU work: paused by {paused.get('by')}" + (f" ({paused['reason']})" if paused.get("reason") else "")
                  + ". Resume with: baabaa gpu resume")
        elif q.get("running"):
            r = q["running"]
            print(f"  GPU: {r.get('label') or r.get('kind')} with {r.get('model')}"
                  + (f", {q['waiting']} waiting" if q.get("waiting") else ""))
        else:
            print("  GPU: idle")
        loaded = [n for n in (health.get("servers") or {}).values() if n]
        if loaded:
            print(f"  Loaded by baabaa itself: {', '.join(loaded)}")
        if health.get("accounts") == 0:
            first = [ln for ln in _startup_lines(log) if "First run" in ln]
            print("  No accounts yet. " + (first[0].strip() if first else "Restart it to get a new setup link."))
    print(f"  Data: {paths.root}\n  Log: {log}")
    return 0
