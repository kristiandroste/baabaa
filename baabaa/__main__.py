"""baabaa command line (`baabaa`, or `python3 -m baabaa` in this folder).

  baabaa start [--port 8443] [--http]   run the server in the background; it outlives this terminal
  baabaa stop | restart | status         stop it, start it again with the same options, or show it
  baabaa serve [--port 8443] [--http]   run the server in this terminal (browser on the LAN + terminal socket)
  baabaa [chat]                          open the terminal UI (the server must be running)
  baabaa account add NAME [--owner] [--password]
  baabaa account list | passwd NAME | remove NAME | grant NAME PATH [--ro]
  baabaa models list | sync | test NAME | approve NAME
  baabaa stats export TABLE [--format csv|jsonl] [--days N] [--out FILE]
  baabaa doctor                          check this machine
  baabaa update [--check] [--now]        get the newest release (installed copies)
  baabaa install [stable|latest|VERSION] install this copy, or a given release
  baabaa uninstall [--purge]             remove the program (--purge: also its data; never Ollama or models)
  baabaa autostart on|off|status         start baabaa with the computer
  baabaa network [local|lan]             this computer only, or the local network too
  baabaa --version
"""

import argparse
import asyncio
import getpass
import os
import secrets
import signal
import sys

KEEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TERM", "SHELL", "PYTHONPATH", "PYTHONUNBUFFERED")


def _clean_env_reexec() -> None:
    """Restart `serve` with a minimal environment, so tool processes cannot read secrets from it."""
    if os.environ.get("BAABAA_CLEAN_ENV") == "1":
        return
    env = {k: v for k, v in os.environ.items()
           if k in KEEP_ENV or k.startswith(("LC_", "XDG_", "BAABAA_"))}
    env["BAABAA_CLEAN_ENV"] = "1"
    from . import BOOT
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.execve(sys.executable, [sys.executable, "-c", BOOT, root] + sys.argv[1:], env)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="baabaa", description="A self-hosted assistant and coding agent on local Ollama models.")
    ap.add_argument("--data", help="data folder (default: $BAABAA_HOME or ~/.local/share/baabaa)")
    ap.add_argument("-v", "--version", action="store_true", help="print the version")
    sub = ap.add_subparsers(dest="cmd")

    for name, text in (("start", "run the server in the background (it keeps running after this terminal closes)"),
                       ("serve", "run the server in this terminal")):
        s = sub.add_parser(name, help=text)
        s.add_argument("--port", type=int, default=int(os.environ.get("BAABAA_PORT", 8443)))
        s.add_argument("--http", action="store_true", help="plain HTTP (no microphone or app install in browsers)")
        s.add_argument("--bind", action="append", help="address to listen on (repeatable; default: loopback + LAN)")
        s.add_argument("--allow", action="append", help="client network in CIDR form, in place of this computer's own subnet (repeatable)")
        s.add_argument("--name", action="append", help="extra host name clients may use (repeatable)")
        s.add_argument("--ca-port", type=int, default=0, help="plain-HTTP port that serves only the CA certificate")
        s.add_argument("--ollama", help="Ollama URL (default http://127.0.0.1:11434)")
    sub.add_parser("stop", help="stop the server")
    sub.add_parser("restart", help="stop the server and start it again in the background, with the same options")
    sub.add_parser("status", help="whether the server is running, where, and what it is doing")

    c = sub.add_parser("chat", help="terminal UI")
    c.add_argument("--account", help="account name")
    c.add_argument("--no-folder", action="store_true", help="plain chat: do not work in the current folder")
    c.add_argument("conversation", nargs="?", help="conversation id to open")

    a = sub.add_parser("account", help="manage accounts")
    a.add_argument("action", choices=["add", "list", "passwd", "remove", "grant", "revoke", "owner", "user"])
    a.add_argument("name", nargs="?")
    a.add_argument("path", nargs="?")
    a.add_argument("--owner", action="store_true")
    a.add_argument("--password", action="store_true", help="ask for a password")
    a.add_argument("--ro", action="store_true", help="read-only grant")

    m = sub.add_parser("models", help="manage models")
    m.add_argument("action", choices=["list", "sync", "test", "approve", "unapprove", "add-llamacpp", "add-sdcpp", "remove"])
    m.add_argument("name", nargs="?")
    m.add_argument("--ctx", type=int)
    m.add_argument("--server", help="add-llamacpp: the llama-server program")
    m.add_argument("--file", help="add-llamacpp: the model file (.gguf)")
    m.add_argument("--lib", action="append", help="add-llamacpp: extra library folder (repeatable)")
    m.add_argument("--mmproj", help="add-llamacpp: vision projector file (.gguf)")
    m.add_argument("--arg", action="append", help="extra server argument (repeatable: --arg=-fa --arg=on)")
    m.add_argument("--diffusion-model", help="add-sdcpp: the diffusion model file")
    m.add_argument("--llm", help="add-sdcpp: the text encoder file")
    m.add_argument("--vae", help="add-sdcpp: the VAE file")
    m.add_argument("--llm-vision", help="add-sdcpp: the text encoder's vision projector (for editing)")
    m.add_argument("--edit", action="store_true", help="add-sdcpp: the model can edit images")
    m.add_argument("--staged", action="store_true", help="add-sdcpp: the model is too large to hold whole, so each part is "
                   "read from disk into VRAM for its step")
    m.add_argument("--steps", type=int, help="add-sdcpp: default sampling steps")
    m.add_argument("--cfg", type=float, help="add-sdcpp: default guidance scale")

    g = sub.add_parser("gpu", help="pause or resume baabaa's use of the GPU")
    g.add_argument("action", choices=["status", "pause", "resume"])
    g.add_argument("--reason", default="")

    st = sub.add_parser("stats", help="usage statistics")
    st.add_argument("action", choices=["export", "summary"])
    st.add_argument("table", nargs="?", default="model_requests")
    st.add_argument("--format", default="csv", choices=["csv", "jsonl"])
    st.add_argument("--days", type=int, default=30)
    st.add_argument("--out")

    dr = sub.add_parser("doctor", help="check this machine")
    dr.add_argument("--sandbox", action="store_true", help="also run commands in the tool sandbox and check that it holds")

    u = sub.add_parser("update", help="install the newest release (an installed copy)")
    u.add_argument("--check", action="store_true", help="only say whether there is one")
    u.add_argument("--now", action="store_true", help="restart the server at once, without waiting for running replies")
    u.add_argument("--no-restart", action="store_true", help="leave the running server as it is")
    i = sub.add_parser("install", help="install this copy, or a release: stable, latest or a version number")
    i.add_argument("target", nargs="?")
    i.add_argument("--channel", choices=["stable", "latest"], help="the release channel for updates")
    i.add_argument("--start", dest="start", action="store_true", default=None, help="start baabaa without asking")
    i.add_argument("--no-start", dest="start", action="store_false", help="do not start baabaa")
    i.add_argument("--autostart", dest="autostart", action="store_true", default=None, help="start baabaa with the computer")
    i.add_argument("--no-autostart", dest="autostart", action="store_false")
    i.add_argument("--lan", dest="lan", action="store_true", default=None, help="let other devices on the network connect")
    i.add_argument("--local", dest="lan", action="store_false", help="this computer only")
    un = sub.add_parser("uninstall", help="remove the program; --purge also deletes baabaa's data (never Ollama or models)")
    un.add_argument("--purge", action="store_true")
    un.add_argument("--yes", action="store_true", help="do not ask")
    au = sub.add_parser("autostart", help="start baabaa with the computer (systemd on Linux, launchd on macOS)")
    au.add_argument("action", choices=["on", "off", "status"])
    sub.add_parser("selfcheck", help=argparse.SUPPRESS)
    nw = sub.add_parser("network", help="who can connect: this computer only (local) or the local network too (lan)")
    nw.add_argument("mode", nargs="?", choices=["local", "lan"])

    args = ap.parse_args(argv)
    if args.version:
        from . import __version__, update
        print(f"baabaa {__version__}" + {"git": " (git checkout)", "folder": " (from a folder)"}.get(update.kind(), ""))
        return
    if args.data:
        os.environ["BAABAA_HOME"] = args.data
    cmd = args.cmd or "chat"
    if cmd in ("start", "stop", "restart", "status"):
        from . import daemon
        if cmd == "start":
            sys.exit(daemon.start(_serve_argv(args)))
        sys.exit({"stop": daemon.stop, "restart": daemon.restart, "status": daemon.status}[cmd]())
    elif cmd == "serve":
        _clean_env_reexec()
        asyncio.run(serve(args))
    elif cmd == "chat":
        from .tui.app import run
        run(getattr(args, "account", None), getattr(args, "conversation", None), not getattr(args, "no_folder", False))
    elif cmd == "account":
        account_cmd(args)
    elif cmd == "models":
        asyncio.run(models_cmd(args))
    elif cmd == "gpu":
        gpu_cmd(args)
    elif cmd == "stats":
        stats_cmd(args)
    elif cmd == "doctor":
        asyncio.run(doctor())
        if args.sandbox:
            from .paths import Paths
            from .sandbox import check
            sys.exit(check.main(Paths().root))
    elif cmd in ("update", "install", "uninstall", "autostart", "selfcheck", "network"):
        from . import installer
        sys.exit({"update": installer.update_cmd, "install": installer.install_cmd, "uninstall": installer.uninstall_cmd,
                  "autostart": installer.autostart_cmd, "network": installer.network_cmd}[cmd](args)
                 if cmd != "selfcheck" else installer.selfcheck_cmd())


def _serve_argv(args) -> list[str]:
    """`start`'s options as `serve` arguments, for the background server (and `restart`)."""
    out = ["--port", str(args.port)]
    if args.http:
        out.append("--http")
    for flag, values in (("--bind", args.bind), ("--allow", args.allow), ("--name", args.name)):
        for v in values or []:
            out += [flag, v]
    if args.ca_port:
        out += ["--ca-port", str(args.ca_port)]
    if args.ollama:
        out += ["--ollama", args.ollama]
    return out


def _port_in_use(hosts, port: int) -> str | None:
    """The first of `hosts` where `port` cannot be bound (something listens there), or None."""
    import socket
    for host in hosts:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
            except OSError:
                return host
    return None


async def serve(args) -> None:
    import shutil
    from .app import App
    from .daemon import hold_lock
    from .paths import Paths
    from .server.api import Web
    from .server.http import Server
    from .server.lan import LanGuard
    from .server import tls

    lock = hold_lock(Paths().root)  # one server per data folder; released when this process ends
    try:
        from .maindb import MainDB
        from .server.lan import saved_mode
        # this computer only, or the local network too (the owner's setting); --bind chooses addresses itself
        local = saved_mode(MainDB(Paths().main_db)) == "local" and not args.bind
        guard = LanGuard(bind=args.bind, networks=args.allow, names=args.name, local_only=local)
        busy = _port_in_use(guard.bind, args.port)
        if busy:
            print(f"baabaa: port {args.port} is already in use on {busy}, by another program or a baabaa with another "
                  f"data folder. Choose another port with --port.", flush=True)
            sys.exit(1)
        app = App(ollama_url=args.ollama)
        await app.startup()
        ctx = None
        if not args.http and not local:  # on this computer only, plain HTTP: browsers treat localhost as secure
            try:
                key, crt = tls.ensure_server_cert(app.paths.tls, guard.hostname, sorted(guard.names), sorted(guard.addresses))
                ctx = tls.server_context(key, crt)
            except tls.TLSError as exc:
                print(f"baabaa: {exc}")
                sys.exit(1)
        setup_token = secrets.token_urlsafe(18) if app.accounts.count() == 0 else None
        web = Web(app, guard, tls=ctx is not None, port=args.port, setup_token=setup_token, plain_http=args.http,
                  forced_bind=bool(args.bind))
        quiet = os.environ.get("BAABAA_ACCESS_LOG") != "1"
        server = Server(web, log=lambda kind, msg: None if (quiet and kind == "access") else print(f"[{kind}] {msg}", flush=True))
        for host in guard.bind:
            await server.listen_tcp(host, args.port, ctx)
        await server.listen_unix(str(app.paths.socket))
        if args.ca_port and ctx is not None:
            await _ca_server(app, guard, args.ca_port, args.port)
    except (Exception, SystemExit) as exc:
        _fall_back(exc)  # after an update that does not start: back to the version it came from
        raise
    urls = guard.urls(args.port, ctx is not None)
    if local:
        print(f"baabaa is running on this computer only: {urls[0]}", flush=True)
        print("  Other devices cannot connect. To let them: baabaa network lan", flush=True)
    else:
        print(f"baabaa is running on the local network: {urls[0]}", flush=True)
        for u in urls[1:]:
            print(f"                                        {u}", flush=True)
    if ctx is not None:
        print(f"  Install the local CA once on each device: {urls[0]}ca.crt  (file: {app.paths.tls / 'ca.crt'})", flush=True)
    if setup_token:
        print(f"  First run: create the owner account at {urls[0]}#setup={setup_token}", flush=True)
    command = "baabaa" if shutil.which("baabaa") else "python3 -m baabaa"
    print(f"  Terminal UI: {command}   (socket {app.paths.socket})", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = [loop.create_task(_housekeeping(app)), loop.create_task(_watch_pause(app)),
             loop.create_task(app.restarter.run(stop)), loop.create_task(app.updates.run())]
    await stop.wait()
    for t in tasks:
        t.cancel()
    await app.shutdown()
    await server.close()
    if app.restarter.exec_root is not None:
        from . import __version__, update
        from .restarter import exec_args, exec_env
        root = app.restarter.exec_root
        fallback = __version__ if update.kind() == "installed" and update.installed_version() != __version__ else None
        print(f"baabaa is restarting into {update._tree_version(root) or 'the same version'} ({root}).", flush=True)
        lock.close()
        os.execve(sys.executable, exec_args(sys.argv[1:], root), exec_env(root, fallback))
    print("baabaa stopped.", flush=True)
    lock.close()


def _fall_back(exc) -> None:
    """A server that a restart started with a new installed version, and that failed to start: switch back
    to the version it came from and start that instead. Nothing happens for any other start."""
    prev = os.environ.get("BAABAA_RESTART_FALLBACK")
    if not prev:
        return
    from . import __version__, update
    from .restarter import exec_args, exec_env
    if update.kind() != "installed" or update.installed_version() == prev:
        return
    try:
        update.switch(prev)
        state = update.load_state()
        state.update(error=f"baabaa {__version__} did not start ({exc!r}), so baabaa went back to {prev}.",
                     failed=__version__, staged=None)
        update.save_state(state)
    except Exception:
        return
    print(f"baabaa {__version__} did not start ({exc!r}); going back to {prev}.", flush=True)
    root = update.target_root()
    os.execve(sys.executable, exec_args(sys.argv[1:], root), exec_env(root, None))


async def _watch_pause(app) -> None:
    """Follow `baabaa gpu pause|resume` from the terminal: the setting is shared through the database."""
    while True:
        await asyncio.sleep(3)
        want = app.maindb.get_setting("gpu_paused")
        if want != app.gateway.queue.paused:
            app.gateway.queue.pause(want)
            if want:
                await app.llamacpp.stop()
                await app.sdcpp.stop()
                try:
                    for entry in await app.ollama.ps():
                        await app.gateway.unload(entry.get("name"))
                except Exception:  # Ollama down: it holds nothing
                    pass


async def _housekeeping(app) -> None:
    from .util import now_ms
    while True:
        days = app.maindb.get_setting("stats_retention_days")
        if days:
            cutoff = now_ms() - int(days) * 86400_000
            for table in ("model_requests", "gpu_samples", "tool_calls", "approvals", "events", "jobs"):
                app.stats.con.execute(f"DELETE FROM {table} WHERE ts_ms < ?", (cutoff,))
        try:
            await app.registry.sync()
            app.ollama_ok = await app.ollama.version()
        except Exception:
            app.ollama_ok = None
        await asyncio.sleep(600)


async def _ca_server(app, guard, port: int, https_port: int) -> None:
    from .server.http import Response, Server

    async def handler(req):
        if req.path == "/ca.crt":
            return Response((app.paths.tls / "ca.crt").read_bytes(), content_type="application/x-x509-ca-cert",
                            headers=[("Content-Disposition", 'attachment; filename="baabaa-local-ca.crt"')])
        target = f"https://{guard.primary_ip}:{https_port}/"
        page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                f"<title>baabaa</title><body style='font:16px system-ui;margin:2em;max-width:36em'>"
                f"<h1>baabaa</h1><p>1. <a href='/ca.crt'>Download the local certificate authority</a> and install it "
                f"(on phones: Settings, search for “certificate”).</p><p>2. Open <a href='{target}'>{target}</a>.</p>")
        return Response(page, content_type="text/html; charset=utf-8")

    async def guarded(req):
        if not guard.allowed_client(req.client_ip):
            return Response("", 403)
        return await handler(req)

    srv = Server(guarded)
    for host in guard.bind:
        await srv.listen_tcp(host, port, None)


def account_cmd(args) -> None:
    from .accounts import AccountError, Accounts
    from .maindb import MainDB
    from .paths import Paths

    paths = Paths()
    accounts = Accounts(MainDB(paths.main_db))
    try:
        if args.action == "list":
            for a in accounts.list():
                flags = [a["role"]] + (["password"] if a["has_password"] else ["no password"]) + (["disabled"] if a["disabled"] else [])
                print(f"{a['name']:<20} {a['display_name']:<24} {', '.join(flags)}")
                for g in accounts.grants(a["id"]):
                    print(f"    {g['access']}  {g['path']}")
            return
        if not args.name:
            sys.exit("give an account name")
        if args.action == "add":
            pw = _ask_password() if args.password else None
            role = "owner" if args.owner or accounts.count() == 0 else "user"
            a = accounts.create(args.name, "", pw, role)
            print(f"Created {a['name']} ({a['role']}).")
            return
        a = accounts.by_name(args.name)
        if a is None:
            sys.exit(f"no account {args.name!r}")
        if args.action == "passwd":
            accounts.set_password(a["id"], _ask_password(allow_empty=True))
            print("Password updated.")
        elif args.action == "remove":
            accounts.delete(a["id"])
            print(f"Removed {a['name']}. (Its files stay in {paths.account(a['id'])} until you delete them.)")
        elif args.action in ("owner", "user"):
            accounts.update(a["id"], role=args.action)
            print(f"{a['name']} is now {args.action}.")
        elif args.action == "grant":
            accounts.grant(a["id"], args.path or ".", "ro" if args.ro else "rw")
            print(f"Granted {'read-only' if args.ro else 'read-write'} access to {os.path.realpath(args.path or '.')}.")
        elif args.action == "revoke":
            accounts.revoke(a["id"], os.path.realpath(args.path or "."))
            print("Revoked.")
    except AccountError as exc:
        sys.exit(str(exc))


def _ask_password(allow_empty: bool = False) -> str | None:
    pw = getpass.getpass("Password (empty for none): " if allow_empty else "Password: ")
    if not pw:
        if allow_empty:
            return None
        sys.exit("empty password")
    if getpass.getpass("Again: ") != pw:
        sys.exit("passwords differ")
    return pw


async def models_cmd(args) -> None:
    from .app import App
    from .ollama import OllamaError
    app = App()
    try:
        await app.registry.sync()
    except (OllamaError, OSError) as exc:
        print(f"(Ollama is not reachable: {exc}; showing what baabaa knows)")
    if args.action in ("list", "sync"):
        for m in app.registry.all():
            fit = m["fit"]
            state = ("approved" if m["approved"] else "fits" if fit.get("fits") else "does not fit" if fit.get("fits") is False
                     else "untested")
            ctx = f"{m['num_ctx'] or fit.get('num_ctx') or '':>7}"
            speed = f"{fit.get('tokens_per_s') or '':>6}"
            print(f"{m['name']:<44} {m['runtime']:<9} {m['role']:<6} {state:<13} ctx {ctx} tok/s {speed}")
        return
    if not args.name:
        sys.exit("give a model name")
    if args.action == "add-llamacpp":
        if not args.server or not args.file:
            sys.exit("add-llamacpp needs --server PATH and --file PATH")
        src = {"server": os.path.abspath(os.path.expanduser(args.server)), "model": os.path.abspath(os.path.expanduser(args.file)),
               "lib_dirs": [os.path.abspath(os.path.expanduser(d)) for d in args.lib or []], "args": args.arg or [],
               "mmproj": os.path.abspath(os.path.expanduser(args.mmproj)) if args.mmproj else None}
        m = app.registry.add_llamacpp(args.name, src)
        info = m["info"]
        print(f"Registered {m['name']}: {info.get('family')} {info.get('parameter_size') or ''}, "
              f"{(info.get('size') or 0) / 2**30:.2f} GiB of weights, native context {info.get('context_length')}, "
              f"capabilities {', '.join(info.get('capabilities') or [])}.")
        print(f"Test its GPU fit when the GPU is free: baabaa models test {m['name']}")
        return
    if args.action == "add-sdcpp":
        if not args.server or not args.diffusion_model:
            sys.exit("add-sdcpp needs --server PATH and --diffusion-model PATH (plus --llm and --vae for most models)")
        ab = lambda p: os.path.abspath(os.path.expanduser(p)) if p else None  # noqa: E731
        m = app.registry.add_sdcpp(args.name, {
            "server": ab(args.server), "diffusion_model": ab(args.diffusion_model), "llm": ab(args.llm), "vae": ab(args.vae),
            "llm_vision": ab(args.llm_vision), "lib_dirs": [ab(d) for d in args.lib or []], "args": args.arg or [],
            "staged": args.staged, "edit": args.edit, "steps": args.steps, "cfg": args.cfg})
        print(f"Registered the image model {m['name']}: {(m['info']['size'] or 0) / 2**30:.2f} GiB of files"
              f"{', staged' if args.staged else ''}{', can edit' if args.edit else ''}.")
        print(f"Test it when the GPU is free: baabaa models test {m['name']}")
        return
    if args.action == "remove":
        m = app.registry.get(args.name)
        if m is None:
            sys.exit(f"no model {args.name}")
        if m["runtime"] == "ollama":
            await app.jobs.remove(args.name)
        else:
            app.registry.remove_external(args.name)
        print(f"Removed {args.name}." + ("" if m["runtime"] == "ollama" else " Its files were left where they are."))
        return
    if args.action == "test":
        job = app.jobs.start_fit(args.name, None)
        await app.jobs.tasks[job]
        print(app.maindb.job_get(job))
    elif args.action == "approve":
        m = app.registry.approve(args.name, True, args.ctx)
        print(f"Approved {m['name']}" + (f" at {m['num_ctx']} tokens of context." if m["role"] != "image" else " (image model)."))
    elif args.action == "unapprove":
        app.registry.approve(args.name, False)
        print("Unapproved.")


def gpu_cmd(args) -> None:
    from .gpu import GPU
    from .maindb import MainDB
    from .paths import Paths
    from .util import now_ms
    db = MainDB(Paths().main_db)
    if args.action == "pause":
        db.set_setting("gpu_paused", {"by": "terminal", "since_ms": now_ms(), "reason": args.reason})
        print("Paused: baabaa starts no GPU work until resumed. Work already running finishes; the models it has "
              "loaded are unloaded within a few seconds.")
    elif args.action == "resume":
        db.set_setting("gpu_paused", None)
        print("Resumed.")
    else:
        p = db.get_setting("gpu_paused")
        print(f"baabaa's GPU work: {'paused by ' + p['by'] + (': ' + p['reason'] if p.get('reason') else '') if p else 'allowed'}")
        from .system import process_name
        g = GPU()
        mem = g.memory()
        if mem and mem.get("used") is not None:
            print(f"{g.name}: {mem['used'] / 2**30:.2f} of {mem['total'] / 2**30:.2f} GiB in use")
        elif mem:
            print(f"{g.name}: models may use about {mem['total'] / 2**30:.0f} GiB of the computer's memory")
        for proc in g.processes() or []:
            print(f"  pid {proc['pid']:>8}  {process_name(proc['pid']):<20} {((proc['used'] or 0) / 2**30):.2f} GiB")


def stats_cmd(args) -> None:
    import time
    from .paths import Paths
    from .stats import Stats
    st = Stats(Paths().stats_db)
    now = int(time.time() * 1000)
    frm = now - args.days * 86400_000
    if args.action == "summary":
        import json
        print(json.dumps(st.summary(None, frm, now + 1, "model"), indent=1))
        return
    out = open(args.out, "wb") if args.out else sys.stdout.buffer
    for chunk in st.export(args.table, args.format, None, frm, now + 1):
        out.write(chunk)
    if args.out:
        out.close()
        print(f"Wrote {args.out}")


async def doctor() -> None:
    import platform
    import shutil
    import sqlite3
    import time
    from .gpu import GPU
    from .ollama import Ollama
    from .sandbox import landlock

    ok = lambda b: "ok " if b else "NO "  # noqa: E731
    from . import __version__, installer, update
    k = update.kind()
    if k == "installed":
        st = update.status(__version__)
        state = update.load_state()
        checked = state.get("checked_ms")
        when = f"last checked {time.strftime('%Y-%m-%d %H:%M', time.localtime(checked / 1000))}" if checked else "not checked yet"
        print(f"ok  baabaa {__version__}, installed in {update.prefix()} ({len(update.versions_on_disk())} version(s) on disk)")
        auto = "off (BAABAA_DISABLE_AUTOUPDATER)" if os.environ.get("BAABAA_DISABLE_AUTOUPDATER") == "1" else \
            "on" if state.get("checks", True) else "off"
        print(f"{'--' if st['latest'] or st.get('error') else 'ok'}  updates: {st['channel']} channel, daily check {auto}, {when}"
              + (f"; baabaa {st['latest']['version']} is available (baabaa update)" if st["latest"] else "")
              + (f"; {st['error']}" if st.get("error") else ""))
    else:
        print(f"--  baabaa {__version__} runs from {update.code_root()} ({'a git checkout' if k == 'git' else 'a folder'}); "
              f"it does not update itself")
    which = shutil.which("baabaa")
    print(f"{'ok ' if which else '-- '} the baabaa command: {which or 'not on PATH (the installer puts it in ~/.local/bin)'}")
    print(f"{'ok ' if installer.managed() else '-- '} start with the computer: {'on' if installer.managed() else 'off (baabaa autostart on)'}")
    print(f"{ok(sys.version_info >= (3, 10))} Python {platform.python_version()}")
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        fts = True
    except sqlite3.Error:
        fts = False
    print(f"{ok(fts)} SQLite {sqlite3.sqlite_version} with FTS5")
    if sys.platform == "darwin":
        from .sandbox import seatbelt
        print(f"{ok(seatbelt.available())} sandbox-exec (the tool sandbox; check it with: baabaa doctor --sandbox)")
    else:
        abi = landlock.abi_version()
        print(f"{ok(abi >= 1)} Landlock ABI {abi} (4+ restricts TCP ports too)")
    if shutil.which("openssl"):  # make a throwaway certificate authority and server certificate, as serve does
        import tempfile
        from pathlib import Path
        from .server import tls
        with tempfile.TemporaryDirectory() as tmp:
            try:
                key, crt = tls.ensure_server_cert(Path(tmp), "doctor", ["localhost"], ["127.0.0.1"])
                tls.server_context(key, crt)
                print("ok  openssl makes baabaa's certificates (HTTPS for other devices)")
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                print(f"NO  openssl could not make baabaa's certificates: {exc}")
    else:
        print("NO  openssl is missing (needed for HTTPS when other devices connect)")
    gpu = GPU()
    print(f"{ok(gpu.available)} GPU: {gpu.name or gpu.error}")
    try:
        v = await Ollama().version()
        print(f"ok  Ollama {v}")
    except Exception as exc:
        print(f"NO  Ollama: {exc}")
    from .ollama import ram_caps_missing, service_environment
    missing = ram_caps_missing(service_environment())
    if missing is None and sys.platform == "darwin":
        pass  # the Ollama app has no service settings to read
    elif missing is None:
        print("--  Ollama's service settings could not be read (not a systemd service)")
    elif missing:
        print("--  Ollama's model server may hold up to 8 GiB of old conversations in system RAM, plus up to 32")
        print("    snapshots each. To cap them, add to its service (sudo systemctl edit ollama), then restart it:")
        for line in missing:
            print(f'      Environment="{line}"')
    else:
        print("ok  Ollama's RAM caches are capped")
    # what this data folder knows: GPU pause, models outside Ollama, and the image and dictation models
    from .maindb import MainDB
    from .models import ModelRegistry
    from .paths import Paths
    db = MainDB(Paths().main_db)
    paused = db.get_setting("gpu_paused")
    print(f"{'--' if paused else 'ok'}  GPU work {'paused by ' + paused['by'] + (': ' + paused['reason'] if paused.get('reason') else '') if paused else 'allowed'}")
    reg = ModelRegistry(db, None)
    for m in reg.all():
        if m["runtime"] == "ollama":
            continue
        src = m["source"]
        files = [src.get(k) for k in ("server", "model", "diffusion_model", "llm", "vae", "llm_vision", "mmproj") if src.get(k)]
        missing = [f for f in files if not os.path.exists(f)]
        state = "approved" if m["approved"] else "fits" if m["fit"].get("fits") else "does not fit" if m["fit"].get("fits") is False else "untested"
        print(f"{ok(not missing)} {m['runtime']} {m['role']} model {m['name']}: {state}" + (f"; missing {', '.join(missing)}" if missing else ""))
    print(f"{'ok ' if reg.image_model() else '-- '} image model: {reg.image_model() or 'none approved'}")
    print(f"{'ok ' if reg.audio_model() else '-- '} dictation model: {reg.audio_model() or 'none approved (an Ollama model that hears audio, such as gemma4)'}")


if __name__ == "__main__":
    main()
