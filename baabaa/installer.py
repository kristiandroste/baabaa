"""The terminal commands that install, update and remove baabaa (update.py does the work):

  baabaa install [stable|latest|VERSION]   install this copy (the installer runs this), or a version from the releases
  baabaa update [--check] [--now] [--no-restart]
  baabaa uninstall [--purge] [--yes]       the program; --purge also baabaa's data, never Ollama or any model
  baabaa autostart on|off|status           start baabaa with the computer (systemd on Linux, launchd on macOS)
  baabaa network [local|lan]               this computer only, or phones and other computers on the network too
"""

import json
import re
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, update

UNIT = "baabaa.service"


# asking on the terminal -------------------------------------------------------------------------------
def ask(question: str, default: bool) -> bool | None:
    """A yes/no question on the terminal, also when stdin is a pipe (`curl … | sh`). None without a terminal."""
    if os.environ.get("BAABAA_ASSUME") in ("yes", "no"):
        return os.environ["BAABAA_ASSUME"] == "yes"
    try:
        tty = open("/dev/tty", "r+")
    except OSError:
        return None
    with tty:
        tty.write(f"{question} [{'Y/n' if default else 'y/N'}] ")
        tty.flush()
        answer = tty.readline().strip().lower()
    return default if not answer else answer in ("y", "yes")


def _progress(prefix_text: str):
    last = [0.0]

    def show(got: int, total: int | None) -> None:
        now = time.monotonic()
        if now - last[0] < 0.2 and (total is None or got < total):
            return
        last[0] = now
        pct = f" {100 * got // total}%" if total else ""
        print(f"\r{prefix_text}{pct} ({got / 1e6:.1f} MB)", end="", flush=True)
    return show


# the running server ------------------------------------------------------------------------------------------
def restart_server(now: bool, reason: str = "update") -> str:
    """Ask this data folder's server (if one runs) to restart into the installed code. Returns what happened."""
    from . import daemon
    from .paths import Paths
    paths = Paths()
    pid = daemon.running_pid(paths.root)
    if not pid:
        return "not running"
    try:
        key = (paths.root / "local.key").read_text().strip()
    except OSError:
        key = ""
    answer = daemon.post(paths.socket, "/api/local/restart", {"now": now, "reason": reason}, {"X-Baabaa-Local-Key": key})
    if answer is not None:
        return "asked"
    state_file = paths.root / "server.json"
    if state_file.exists():  # an older server that cannot restart itself, started with `baabaa start`
        daemon.restart()
        return "restarted"
    return "manual"


def _say_restart(result: str, now: bool) -> None:
    if result == "asked":
        print("The server restarts now." if now else
              "The server restarts as soon as the replies it is writing are finished (`baabaa update --now` does not wait).")
    elif result == "manual":
        print("Restart the server to use the new version: baabaa restart")


# install ------------------------------------------------------------------------------------------------------
def install_cmd(args) -> int:
    target = args.target
    k = update.kind()
    if args.channel:
        update.set_preferences(channel=args.channel)
    first = update.installed_version() is None
    from .paths import data_dir
    new_data = not (data_dir() / "baabaa.db").exists()
    try:
        if target:  # a version from the release list
            if target in update.CHANNELS:
                update.set_preferences(channel=target)
            print(f"Getting baabaa {target}…", flush=True)
            r = update.fetch_version(target, _progress("Downloading"))
            print()
            version = r["version"]
        elif k == "installed":
            version = __version__
        else:  # this copy: the installer's unpacked release, or a folder someone runs from
            version = __version__
            ok, out = update.selfcheck(update.code_root())
            if not ok:
                print(f"This copy of baabaa does not pass its start-up check:\n{out}")
                return 1
            update.copy_tree(update.code_root(), version)
    except update.UpdateError as exc:
        print(f"\n{exc}")
        return 1
    before = update.installed_version()
    if before != version:
        from .paths import Paths
        if before:
            update.backup_databases(Paths().root, before)
        update.switch(version)
    link, how = update.ensure_launcher()
    update.cleanup({__version__})
    print(f"baabaa {version} is installed in {update.prefix()}.")
    if how == "custom":
        print(f"  {link} is a command of your own, so it was left as it is. The installed baabaa is "
              f"{update.prefix() / 'current' / 'bin' / 'baabaa'}.")
    if not _on_path(link.parent):
        shell = os.path.basename(os.environ.get("SHELL", ""))
        rc = ("~/.zprofile" if sys.platform == "darwin" else "~/.zshrc") if shell == "zsh" else "~/.bashrc"
        print(f"  {link.parent} is not on your PATH yet. Add it, then open a new terminal:\n"
              f"    echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> {rc}")
    if not first and before != version:
        result = restart_server(now=False)
        _say_restart(result, False)
        return 0
    _checks()
    from . import daemon
    from .paths import Paths
    if new_data:  # a new data folder: who may connect, asked once (`baabaa network` changes it later)
        lan = args.lan if args.lan is not None else ask("Let phones and other computers on your network use baabaa?", False)
        from .maindb import MainDB
        MainDB(Paths().main_db).set_setting("network", "lan" if lan else "local")
        if not lan:
            print("baabaa accepts this computer only. To let other devices in later: baabaa network lan")
    if daemon.running_pid(Paths().root):
        print("baabaa is already running. Restart it to use this version: baabaa restart")
        return 0
    start = args.start if args.start is not None else ask("Start baabaa now?", True)
    if start is None:
        print("Start it with: baabaa start")
        return 0
    if start:
        launcher = update.prefix() / "current" / "bin" / "baabaa"
        res = subprocess.run([str(launcher), "start"])
        if res.returncode != 0:
            return res.returncode
    else:
        print("Start it later with: baabaa start")
    auto = args.autostart if args.autostart is not None else ask("Start baabaa whenever this computer starts?", False)
    if auto:
        autostart_on([], start_now=False)
    elif auto is False:
        print("To start it with the computer later: baabaa autostart on")
    return 0


def _on_path(d: Path) -> bool:
    want = os.path.realpath(d)
    return any(os.path.realpath(p) == want for p in os.environ.get("PATH", "").split(os.pathsep) if p)


def _checks() -> None:
    """What baabaa needs besides itself: Ollama, an NVIDIA GPU, HTTPS."""
    import asyncio
    from .gpu import GPU
    from .ollama import Ollama
    try:
        v = asyncio.run(asyncio.wait_for(Ollama(_ollama_url()).version(), 5))
        print(f"  Ollama {v} is running.")
    except Exception:
        if sys.platform == "darwin":
            print("  Ollama is not running here. baabaa needs it for its models: install the Ollama app from\n"
                  "    https://ollama.com/download and open it once.")
        else:
            print("  Ollama is not running here. baabaa needs it for its models; install it with:\n"
                  "    curl -fsSL https://ollama.com/install.sh | sh")
    gpu = GPU()
    if gpu.available:
        mem = gpu.memory() or {}
        share = f" (models may use about {mem['total'] / 2**30:.0f} GB of the computer's memory)" if gpu.kind == "apple" and mem.get("total") else ""
        print(f"  GPU: {gpu.name}{share}.")
    elif sys.platform == "darwin":
        print(f"  This Mac cannot run models with baabaa: {gpu.error}. The rest of baabaa works.")
    else:
        print("  No NVIDIA GPU was found. baabaa runs models only on the GPU, so it cannot run any here.")
    if not shutil.which("openssl"):
        print("  openssl is missing: baabaa needs it for HTTPS on your network (or run it with --http).")


# update ---------------------------------------------------------------------------------------------------------
def update_cmd(args) -> int:
    why = update.disabled()
    if why:
        print(why)
        return 1
    state = update.load_state()
    channel = state.get("channel") or "stable"
    current = update.installed_version()
    try:
        index = update.fetch_releases()
    except update.NoReleases:
        print("No releases are published yet.")
        return 0
    except update.UpdateError as exc:
        print(exc)
        return 1
    r = update.pick(index, channel)
    state.update(checked_ms=int(time.time() * 1000), latest=update.summary(r), error=None, published=True)
    update.save_state(state)
    running = _server_version()
    skipped = r is not None and r["version"] == state.get("failed")
    if not r or not update.newer(r["version"], current) or skipped:
        print(f"baabaa is up to date ({current}, {channel} channel)."
              + (f" {r['version']} did not start on this computer, so it is skipped until a newer release." if skipped else ""))
        if running and current and running != current and not args.check:
            if args.no_restart:
                print(f"The server still runs {running}; restart it to use {current}: baabaa restart")
            else:
                _say_restart(restart_server(now=args.now), args.now)
        return 0
    if args.check:
        print(f"baabaa {r['version']} is available ({channel} channel; this is {current}). Install it with: baabaa update")
        for note in (r.get("notes") or [])[:8]:
            print(f"  - {note}")
        return 0
    try:
        update.stage(index, r, _progress(f"Downloading baabaa {r['version']}"))
        print()
    except update.UpdateError as exc:
        print(f"\n{exc}")
        return 1
    from .paths import Paths
    update.backup_databases(Paths().root, current or __version__)
    update.switch(r["version"])
    update.cleanup({v for v in (running, __version__) if v})
    print(f"Updated baabaa from {current} to {r['version']}.")
    for note in (r.get("notes") or [])[:8]:
        print(f"  - {note}")
    if args.no_restart:
        if running:
            print("The server still runs the old version; restart it to use the new one: baabaa restart")
        return 0
    _say_restart(restart_server(now=args.now), args.now)
    return 0


def _server_version() -> str | None:
    from . import daemon
    from .paths import Paths
    paths = Paths()
    if not daemon.running_pid(paths.root):
        return None
    health = daemon.ask(paths.socket)
    return (health or {}).get("version")


# uninstall ---------------------------------------------------------------------------------------------------
def uninstall_cmd(args) -> int:
    from . import daemon
    from .paths import data_dir
    k = update.kind()
    data = data_dir()
    if args.purge:
        if not _purge_plan(data):
            return 1
        if not args.yes:
            answer = None
            try:
                with open("/dev/tty", "r+") as tty:
                    tty.write("Type delete to delete baabaa's data: ")
                    tty.flush()
                    answer = tty.readline().strip()
            except OSError:
                print("No terminal to confirm on: run it again with --yes to delete without asking.")
                return 1
            if answer != "delete":
                print("Nothing was deleted.")
                return 1
    elif not args.yes:
        ok = ask(f"Remove the baabaa program? Your conversations and settings stay in {data}.", True)
        if not ok:
            print("Nothing was removed.")
            return 1
    daemon.stop(quiet=True)
    if _unit_path().exists():
        autostart_off(quiet=True)
    link = update.bin_dir() / "baabaa"
    if link.is_symlink() and str(os.readlink(link)).startswith(str(update.prefix())):
        link.unlink()
    removed_program = False
    if update.prefix().exists():
        shutil.rmtree(update.prefix(), ignore_errors=True)
        removed_program = True
    if removed_program:
        print(f"Removed the baabaa program ({update.prefix()}).")
    if k != "installed":
        print(f"This baabaa runs from {update.code_root()}; delete that folder yourself if you no longer want it.")
    if args.purge:
        shutil.rmtree(data, ignore_errors=True)
        print(f"Deleted baabaa's data ({data}).")
    else:
        print(f"Your data stays in {data}; delete that folder if you no longer want it.")
    return 0


def _purge_plan(data: Path) -> bool:
    """Say what --purge deletes and what it keeps. False when the data folder does not look like baabaa's."""
    home = Path.home().resolve()
    if data.resolve() in (home, Path("/").resolve()) or not ((data / "baabaa.db").exists() or (data / "server.lock").exists()):
        print(f"{data} does not look like a baabaa data folder; nothing was deleted.")
        return False
    size = sum(f.stat().st_size for f in data.rglob("*") if f.is_file() and not f.is_symlink())
    print(f"This deletes baabaa's data in {data} ({size / 1e6:.0f} MB): accounts, conversations, files, memory,\n"
          "projects, settings, logs and usage statistics, and the program itself.")
    print("It does not touch Ollama: the program, its service and settings, and its models stay as they are.")
    models = _ollama_models(data)
    if models:
        print("  Ollama's models, which stay (remove one with: ollama rm NAME):")
        for name in models:
            print(f"    {name}")
    for line in _external_models(data):
        print(line)
    return True


def _ollama_url(data: Path | None = None) -> str:
    """The Ollama this baabaa uses: $BAABAA_OLLAMA_URL, the data folder's setting, or the default."""
    if os.environ.get("BAABAA_OLLAMA_URL"):
        return os.environ["BAABAA_OLLAMA_URL"]
    if data is not None:
        import sqlite3
        try:
            con = sqlite3.connect(f"file:{data / 'baabaa.db'}?mode=ro", uri=True)
            row = con.execute("SELECT value FROM settings WHERE key='ollama_url'").fetchone()
            con.close()
            if row:
                return json.loads(row[0])
        except (sqlite3.Error, ValueError):
            pass
    return "http://127.0.0.1:11434"


def _ollama_models(data: Path | None = None) -> list[str]:
    import urllib.request
    try:
        with urllib.request.urlopen(_ollama_url(data).rstrip("/") + "/api/tags", timeout=3) as r:
            return sorted(m.get("name", "?") for m in json.load(r).get("models", []))
    except Exception:
        return []


def _external_models(data: Path) -> list[str]:
    """Model programs and files outside Ollama that baabaa used where they are: they stay too."""
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{data / 'baabaa.db'}?mode=ro", uri=True)
        rows = con.execute("SELECT name, source FROM models WHERE runtime != 'ollama'").fetchall()
        con.close()
    except sqlite3.Error:
        return []
    out = []
    for name, source in rows:
        try:
            src = json.loads(source)
        except ValueError:
            continue
        files = sorted({os.path.dirname(v) for k, v in src.items() if isinstance(v, str) and v.startswith("/")})
        if files:
            out.append(f"  {name}: its files stay in {', '.join(files)}")
    if out:
        out.insert(0, "  Model programs and files baabaa used in place, which stay:")
    return out


# who can connect ------------------------------------------------------------------------------------------------
MODE_TEXT = {"local": "this computer only", "lan": "this computer and other devices on your local network"}


def network_cmd(args) -> int:
    from .accounts import Accounts
    from .maindb import MainDB
    from .paths import Paths
    from .server.lan import saved_mode
    paths = Paths()
    db = MainDB(paths.main_db)
    saved = saved_mode(db)
    health = _health()
    running = (health or {}).get("network")
    if not args.mode:
        print(f"baabaa accepts connections from {MODE_TEXT[saved]}.")
        if running and running != saved:
            print(f"The server running now still accepts {MODE_TEXT[running]}; that changes when it restarts (baabaa restart).")
        print("Change it with: baabaa network local   or   baabaa network lan")
        return 0
    db.set_setting("network", args.mode)
    print(f"baabaa now accepts connections from {MODE_TEXT[args.mode]}.")
    if args.mode == "lan":
        open_accounts = [a["display_name"] for a in Accounts(db).list() if not a["has_password"] and not a["disabled"]]
        if open_accounts:
            print(f"  These accounts have no password, so anyone on your network could open them: {', '.join(open_accounts)}.\n"
                  "  Give them one: baabaa account passwd NAME")
    if running and running != args.mode:
        result = restart_server(now=False, reason="network")
        if result == "asked":
            print("The server restarts as soon as the replies it is writing are finished.")
        elif result == "manual":
            print("Restart the server for it to take effect: baabaa restart")
    return 0


def _health() -> dict | None:
    from . import daemon
    from .paths import Paths
    paths = Paths()
    return daemon.ask(paths.socket) if daemon.running_pid(paths.root) else None


# start with the computer --------------------------------------------------------------------------------------
# Linux: a systemd user service. macOS: a launchd agent (~/Library/LaunchAgents), which starts at login.
LABEL = "io.github.kristiandroste.baabaa"


def _unit_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "systemd" / "user" / UNIT


def _systemctl(*args, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=check, timeout=30)


def _launchctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=30)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def unit_text(launcher: str, serve_args: list[str], data: str | None) -> str:
    import shlex
    env = f"Environment=BAABAA_HOME={shlex.quote(data)}\n" if data else ""
    command = " ".join(shlex.quote(a) for a in [launcher, "serve", *serve_args])
    return ("[Unit]\nDescription=baabaa, a local assistant on your own GPU\nAfter=network-online.target\n"
            "Wants=network-online.target\n\n[Service]\n" + env + f"ExecStart={command}\n"
            "Restart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n")


def plist_bytes(launcher: str, serve_args: list[str], data: str | None, log: str) -> bytes:
    """The launchd agent: runs `baabaa serve` at login, again if it ends with an error, output to the log."""
    import plistlib
    agent = {"Label": LABEL, "ProgramArguments": [launcher, "serve", *serve_args], "RunAtLoad": True,
             "KeepAlive": {"SuccessfulExit": False}, "StandardOutPath": log, "StandardErrorPath": log,
             "ProcessType": "Interactive"}
    if data:
        agent["EnvironmentVariables"] = {"BAABAA_HOME": data}
    return plistlib.dumps(agent)


def managed() -> bool:
    """True when the computer's service manager starts baabaa: start, stop and restart go through it."""
    if not _unit_path().exists():
        return False
    try:
        if sys.platform == "darwin":
            return _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0
        if not shutil.which("systemctl"):
            return False
        return _systemctl("is-enabled", UNIT).stdout.strip() == "enabled"
    except (OSError, subprocess.SubprocessError):
        return False


def service(action: str) -> tuple[int, str]:
    """start, stop or restart baabaa through the service manager: (exit status, message)."""
    if sys.platform == "darwin":
        target = f"{_domain()}/{LABEL}"
        args = {"start": ["kickstart", target], "restart": ["kickstart", "-k", target], "stop": ["kill", "SIGTERM", target]}[action]
        res = _launchctl(*args)
    else:
        res = _systemctl(action, UNIT)
    return res.returncode, (res.stderr or res.stdout).strip()


def service_main_pid() -> int | None:
    """The process the service manager runs for baabaa, if any."""
    try:
        if sys.platform == "darwin":
            m = re.search(r"^\s*pid = (\d+)", _launchctl("print", f"{_domain()}/{LABEL}").stdout, re.M)
            return int(m.group(1)) if m else None
        out = _systemctl("show", UNIT, "-p", "MainPID").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    pid = out.partition("=")[2]
    return int(pid) if pid.isdigit() and pid != "0" else None


def service_log() -> str:
    return "journalctl --user -u baabaa" if sys.platform != "darwin" else "the server log (baabaa status shows where)"


def autostart_on(serve_args: list[str], start_now: bool = True) -> int:
    mac = sys.platform == "darwin"
    if not mac and (sys.platform != "linux" or not shutil.which("systemctl")):
        print("Starting with the computer needs systemd on Linux, or macOS.")
        return 1
    from . import daemon
    from .paths import Paths
    paths = Paths()
    if not serve_args:  # the options of a server started with `baabaa start`
        try:
            serve_args = json.loads((paths.root / "server.json").read_text()).get("args") or []
        except (OSError, ValueError):
            serve_args = []
    if update.kind() == "installed":
        launcher = str(update.prefix() / "current" / "bin" / "baabaa")
    else:
        launcher = str(update.code_root() / "bin" / "baabaa")
    data = str(paths.root) if os.environ.get("BAABAA_HOME") else None
    unit = _unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    running = daemon.running_pid(paths.root)
    if mac:
        (paths.root / "logs").mkdir(exist_ok=True, mode=0o700)
        unit.write_bytes(plist_bytes(launcher, serve_args, data, str(paths.root / "logs" / "server.log")))
        print("baabaa now starts whenever you log in.")
        if running:
            print("The server running now was started by hand; from the next login on, macOS starts it.")
        elif start_now:
            _launchctl("bootout", f"{_domain()}/{LABEL}")
            res = _launchctl("bootstrap", _domain(), str(unit))
            if res.returncode != 0:
                print(f"macOS did not start it now ({res.stderr.strip()}); it starts at the next login.")
        return 0
    unit.write_text(unit_text(launcher, serve_args, data))
    _systemctl("daemon-reload")
    res = _systemctl("enable", UNIT)
    if res.returncode != 0:
        print(f"systemd did not accept the service: {res.stderr.strip()}")
        return 1
    print("baabaa now starts whenever you log in.")
    user = os.environ.get("USER") or ""
    linger = subprocess.run(["loginctl", "show-user", user, "-p", "Linger"], capture_output=True, text=True)
    if "Linger=yes" not in linger.stdout:
        res = subprocess.run(["loginctl", "enable-linger", user], capture_output=True, text=True)
        if res.returncode == 0:
            print("It also starts when the computer starts, before anyone logs in.")
        else:
            print(f"To have it start before anyone logs in, run once: sudo loginctl enable-linger {user}")
    if running:
        print("The server running now was started by hand; from the next start on, systemd starts it.")
    elif start_now:
        _systemctl("start", UNIT)
    return 0


def autostart_off(quiet: bool = False) -> int:
    if sys.platform != "darwin" and shutil.which("systemctl"):
        _systemctl("disable", UNIT)
    _unit_path().unlink(missing_ok=True)  # macOS: the agent is not loaded at the next login
    if sys.platform != "darwin" and shutil.which("systemctl"):
        _systemctl("daemon-reload")
    if not quiet:
        print("baabaa no longer starts with the computer. (A server running now keeps running: baabaa stop.)")
    return 0


def autostart_cmd(args) -> int:
    if args.action == "on":
        return autostart_on([])
    if args.action == "off":
        return autostart_off()
    print("baabaa starts with the computer." if managed() else "baabaa does not start with the computer (baabaa autostart on).")
    return 0


# checks ------------------------------------------------------------------------------------------------------
def selfcheck_cmd() -> int:
    """Import every module of this copy and look for the web app's files: a new version must pass this
    before baabaa switches to it or restarts into it."""
    import importlib
    import pkgutil
    import traceback
    import baabaa
    failed = []
    for m in pkgutil.walk_packages(baabaa.__path__, "baabaa."):
        try:
            importlib.import_module(m.name)
        except Exception:
            failed.append(f"{m.name}:\n{traceback.format_exc(limit=3)}")
    web = Path(baabaa.__file__).parent / "web"
    for f in ("index.html", "js/app.js", "css/app.css"):
        if not (web / f).exists():
            failed.append(f"missing web/{f}")
    if failed:
        print("\n".join(failed))
        return 1
    print(f"ok {__version__}")
    return 0
