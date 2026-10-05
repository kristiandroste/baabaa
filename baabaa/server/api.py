"""The HTTP API and the web app's entry point. Everything a window can do goes through here."""

import asyncio
import json
import mimetypes
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

from .. import __version__, docs
from ..accounts import AccountError
from ..agent import permissions
from ..gateway import ModelNotAllowed, ResidencyError
from ..ollama import OllamaError
from ..scout import CATEGORIES as SCOUT_CATEGORIES
from ..util import clip, new_id, now_ms
from .http import HTTPError, Request, Response, Router, StreamResponse, json_response, sse_event, static_response

WEB = Path(__file__).resolve().parent.parent / "web"
COOKIE = "baabaa"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
PUBLIC = {("GET", "/api/profiles"), ("POST", "/api/login"), ("GET", "/api/setup"), ("POST", "/api/setup"),
          ("GET", "/api/health"), ("GET", "/ca.crt")}

INLINE_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp"}
APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
           "media-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; frame-src 'self' blob: data:; "
           "worker-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# Artifacts are model-written: an opaque origin with no network at all.
ARTIFACT_CSP = ("default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' data: blob:; style-src 'unsafe-inline' data:; "
                "img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; form-action 'none'; "
                "frame-ancestors 'self'; sandbox allow-scripts allow-modals allow-downloads")


class Web:
    def __init__(self, app, guard, tls: bool, port: int, setup_token: str | None = None, log=print, plain_http: bool = False,
                 forced_bind: bool = False):
        self.app = app
        self.guard = guard
        self.tls = tls
        self.port = port
        self.plain_http = plain_http  # served with --http: no TLS on the network either
        self.forced_bind = forced_bind  # started with --bind: the addresses were chosen by hand
        self.setup_token = setup_token
        self.log = log
        self.router = Router()
        self._routes()
        app.web = self

    def network_state(self) -> dict:
        """The saved network mode, the one this server runs in, and its addresses now and after a restart."""
        from .lan import LanGuard, saved_mode
        saved = saved_mode(self.app.maindb)
        running = "local" if self.guard.local_only else "lan"
        out = {"saved": saved, "running": running, "urls": self.guard.urls(self.port, self.tls), "next_urls": None,
               "forced": self.forced_bind}
        if saved != running and not self.forced_bind:
            if saved == "local":
                out["next_urls"] = [f"http://localhost:{self.port}/"]
            else:
                out["next_urls"] = LanGuard().urls(self.port, not self.plain_http)
        return out

    # entry --------------------------------------------------------------------------------------
    async def __call__(self, req: Request):
        if not self.guard.allowed_client(req.client_ip):
            return Response({"error": "baabaa serves the local network only"}, 403)
        if req.transport == "tcp" and not self.guard.allowed_host(req.headers.get("host")):
            return Response({"error": "Unknown host name"}, 421)
        try:
            resp = await self._dispatch(req)
        except (AccountError, ValueError) as exc:
            resp = Response({"error": str(exc)}, 400)
        except PermissionError as exc:
            resp = Response({"error": str(exc) or "Not allowed"}, 403)
        except KeyError as exc:
            resp = Response({"error": f"Not found: {exc}"}, 404)
        except (ModelNotAllowed, ResidencyError) as exc:
            resp = Response({"error": str(exc)}, 409)
        except OllamaError as exc:
            resp = Response({"error": f"Ollama: {exc}"}, 502)
        except RuntimeError as exc:
            resp = Response({"error": str(exc)}, 409)
        self._secure(req, resp)
        return resp

    def _secure(self, req, resp) -> None:
        h = resp.headers
        if not h.get("content-security-policy"):
            h.set("Content-Security-Policy", APP_CSP)
            h.set("X-Frame-Options", "DENY")
        h.set("X-Content-Type-Options", "nosniff")
        h.set("Referrer-Policy", "no-referrer")
        h.set("Permissions-Policy", "microphone=(self), camera=(self), geolocation=()")
        if req.path.startswith("/api/"):
            h.set("Cache-Control", "no-store")

    async def _dispatch(self, req: Request):
        handler, params = self.router.match(req.method, req.path)
        if handler is None:
            if req.path.startswith("/api/"):
                raise HTTPError(404)
            return self._static(req)
        req.params = params
        if req.path.startswith("/api/local/"):  # the local terminal's own commands (`baabaa update`)
            if req.transport != "unix" or not secrets.compare_digest(req.headers.get("x-baabaa-local-key") or "", self.app.local_key):
                raise HTTPError(403, "Only the terminal on this computer can do this")
            req.account, req.session = None, {}
            return await handler(req)
        if (req.method, req.path) not in PUBLIC and not req.path.startswith("/api/setup"):
            self._authenticate(req)
        return await handler(req)

    def _authenticate(self, req: Request) -> None:
        token, via_cookie = None, False
        auth = req.headers.get("authorization") or ""
        if auth.startswith("Bearer bk_"):
            account = self.app.accounts.api_key_account(auth[7:].strip())
            if account is None:
                raise HTTPError(401, "Unknown or revoked API key")
            req.session = {"csrf": None, "api_key": account.pop("_key_id"), "account": account}
            req.account = account
            return
        if auth.startswith("Bearer ") and req.transport == "unix":
            token = auth[7:].strip()
        else:
            token, via_cookie = req.cookie(COOKIE), True
        session = self.app.accounts.session(token) if token else None
        if session is None:
            raise HTTPError(401, "Sign in first")
        if via_cookie and req.method in UNSAFE:
            if not self.guard.allowed_origin(req.headers.get("origin")):
                raise HTTPError(403, "Cross-site request refused")
            if not secrets.compare_digest(req.headers.get("x-csrf-token") or "", session["csrf"]):
                raise HTTPError(403, "Missing or wrong CSRF token")
        req.session = session
        req.account = session["account"]

    def _static(self, req: Request):
        path = req.path
        if path in ("/", "/index.html") or not ("." in path.rsplit("/", 1)[-1]):
            return static_response(WEB, "index.html", req)
        return static_response(WEB, path, req)

    # routes -------------------------------------------------------------------------------------
    def _routes(self) -> None:
        r = self.router.add
        r("GET", "/api/health", self.health)
        r("GET", "/ca.crt", self.ca_cert)
        r("GET", "/api/setup", self.setup_info)
        r("POST", "/api/setup", self.setup)
        r("GET", "/api/profiles", self.profiles)
        r("POST", "/api/login", self.login)
        r("POST", "/api/logout", self.logout)
        r("GET", "/api/me", self.me)
        r("PATCH", "/api/me", self.update_me)
        r("POST", "/api/me/password", self.change_password)
        r("GET", "/api/me/keys", self.list_keys)
        r("POST", "/api/me/keys", self.create_key)
        r("DELETE", "/api/me/keys/{kid}", self.revoke_key)
        r("GET", "/api/status", self.status)
        r("GET", "/api/events", self.events)
        # conversations
        r("GET", "/api/conversations", self.list_conversations)
        r("POST", "/api/conversations", self.create_conversation)
        r("GET", "/api/conversations/{cid}", self.get_conversation)
        r("PATCH", "/api/conversations/{cid}", self.update_conversation)
        r("DELETE", "/api/conversations/{cid}", self.delete_conversation)
        r("POST", "/api/conversations/{cid}/messages", self.send_message)
        r("POST", "/api/conversations/{cid}/stop", self.stop)
        r("POST", "/api/conversations/{cid}/regenerate", self.regenerate)
        r("POST", "/api/conversations/{cid}/branch", self.branch)
        r("POST", "/api/conversations/{cid}/compact", self.compact)
        r("POST", "/api/conversations/{cid}/rewind", self.rewind)
        r("POST", "/api/conversations/{cid}/fork", self.fork)
        r("POST", "/api/conversations/{cid}/feedback", self.feedback)
        r("GET", "/api/conversations/{cid}/export", self.export)
        r("GET", "/api/conversations/{cid}/shells", self.shells)
        r("GET", "/api/conversations/{cid}/files", self.conv_files)
        r("POST", "/api/conversations/{cid}/share", self.share_conversation)
        r("POST", "/api/conversations/{cid}/worktree", self.add_worktree)
        r("DELETE", "/api/conversations/{cid}/worktree", self.remove_worktree)
        r("GET", "/api/shares", self.list_shares)
        r("GET", "/api/shares/{shid}", self.get_share)
        r("POST", "/api/shares/{shid}/copy", self.copy_share)
        r("DELETE", "/api/shares/{shid}", self.delete_share)
        r("GET", "/api/search", self.search)
        # projects and memory
        r("GET", "/api/projects", self.list_projects)
        r("POST", "/api/projects", self.create_project)
        r("GET", "/api/projects/{pid}", self.get_project)
        r("PATCH", "/api/projects/{pid}", self.update_project)
        r("DELETE", "/api/projects/{pid}", self.delete_project)
        r("POST", "/api/projects/{pid}/files", self.add_project_file)
        r("POST", "/api/projects/{pid}/text", self.add_project_text)
        r("GET", "/api/projects/{pid}/files/{fid}", self.get_project_file)
        r("DELETE", "/api/projects/{pid}/files/{fid}", self.delete_project_file)
        r("GET", "/api/projects/{pid}/search", self.search_project)
        r("GET", "/api/schedules", self.list_schedules)
        r("POST", "/api/schedules", self.create_schedule)
        r("PATCH", "/api/schedules/{sid}", self.update_schedule)
        r("DELETE", "/api/schedules/{sid}", self.delete_schedule)
        r("POST", "/api/schedules/{sid}/run", self.run_schedule)
        r("GET", "/api/memory", self.list_memory)
        r("POST", "/api/memory", self.add_memory)
        r("PATCH", "/api/memory/{mid}", self.update_memory)
        r("DELETE", "/api/memory/{mid}", self.delete_memory)
        r("POST", "/api/pending/{pid}", self.answer_pending)
        # artifacts and files
        r("GET", "/api/artifacts", self.list_artifacts)
        r("GET", "/api/images", self.list_images)
        r("GET", "/api/artifacts/{aid}", self.get_artifact)
        r("GET", "/api/artifacts/{aid}/export", self.export_artifact)
        r("GET", "/artifact-frame/{aid}", self.artifact_frame)
        r("POST", "/api/uploads", self.upload)
        r("POST", "/api/transcribe", self.transcribe)
        r("GET", "/api/attachments/{aid}", self.get_attachment)
        r("GET", "/api/folders", self.browse_folders)
        r("POST", "/api/folders/trust", self.trust_folder)
        # rules
        r("GET", "/api/rules", self.get_rules)
        r("POST", "/api/rules", self.add_rule)
        r("DELETE", "/api/rules/{rid}", self.delete_rule)
        # customization (skills, commands, agents, styles, hooks)
        r("GET", "/api/extend", self.extend_list)
        r("GET", "/api/extend/{kind}/{name}", self.extend_get)
        r("PUT", "/api/extend/{kind}/{name}", self.extend_put)
        r("DELETE", "/api/extend/{kind}/{name}", self.extend_delete)
        # connectors (MCP)
        r("GET", "/api/connectors", self.connectors)
        r("POST", "/api/connectors", self.add_connector)
        r("PATCH", "/api/connectors/{sid}", self.update_connector)
        r("DELETE", "/api/connectors/{sid}", self.remove_connector)
        r("POST", "/api/connectors/{sid}/test", self.test_connector)
        # models
        r("GET", "/api/models", self.models)
        r("POST", "/api/models/sync", self.models_sync)
        r("POST", "/api/models/test", self.models_test)
        r("POST", "/api/models/approve", self.models_approve)
        r("POST", "/api/models/pull", self.models_pull)
        r("POST", "/api/models/remove", self.models_remove)
        r("POST", "/api/models/scout", self.models_scout)
        r("POST", "/api/models/scout/dismiss", self.models_scout_dismiss)
        r("POST", "/api/models/external", self.models_external)
        r("POST", "/api/gpu/pause", self.gpu_pause)
        r("GET", "/api/jobs", self.jobs)
        r("POST", "/api/jobs/{jid}/cancel", self.cancel_job)
        r("GET", "/api/settings", self.machine_settings)
        r("PATCH", "/api/settings", self.update_machine_settings)
        r("GET", "/api/update", self.update_status)
        r("POST", "/api/update/check", self.update_check)
        r("POST", "/api/update/install", self.update_install)
        r("POST", "/api/update/previous", self.update_previous)
        r("PATCH", "/api/update/settings", self.update_settings)
        r("POST", "/api/restart", self.restart)
        r("DELETE", "/api/restart", self.cancel_restart)
        r("POST", "/api/local/restart", self.local_restart)
        r("GET", "/api/network", self.network)
        r("PATCH", "/api/network", self.set_network)
        # statistics
        r("GET", "/api/stats/summary", self.stats_summary)
        r("GET", "/api/stats/gpu", self.stats_gpu)
        r("GET", "/api/stats/export", self.stats_export)
        r("GET", "/api/stats/snapshot", self.stats_snapshot)
        # accounts (owner)
        r("GET", "/api/accounts", self.list_accounts)
        r("POST", "/api/accounts", self.create_account)
        r("PATCH", "/api/accounts/{aid}", self.update_account)
        r("DELETE", "/api/accounts/{aid}", self.delete_account)
        r("POST", "/api/accounts/{aid}/grants", self.add_grant)
        r("DELETE", "/api/accounts/{aid}/grants", self.remove_grant)

    # helpers ------------------------------------------------------------------------------------
    def _owner(self, req) -> None:
        if req.account["role"] != "owner":
            raise PermissionError("Only an owner can do this")

    def _store(self, req):
        return self.app.stores.get(req.account["id"])

    def _confirm_host_access(self, req, body: dict) -> None:
        """Adding a program that runs on this computer needs more than a session: the local terminal, or
        an owner account with a password, typed again for this request."""
        self._owner(req)
        if req.transport == "unix":
            return
        if req.session.get("api_key"):
            raise PermissionError("API keys cannot add programs that run on this computer")
        if not self.app.accounts.get(req.account["id"]).get("has_password"):
            raise PermissionError("Only an owner account with a password can add programs that run on this computer. "
                                  "Set a password in Settings > General first.")
        if not self.app.accounts.check_password(req.account["id"], body.get("password") or "", req.client_ip):
            raise HTTPError(401, "The password is wrong")

    def _conv(self, req):
        conv = self._store(req).conversation(req.params["cid"])
        if conv is None:
            raise KeyError(req.params["cid"])
        return conv

    def _cookie(self, token: str, max_age: int) -> str:
        secure = "; Secure" if self.tls else ""
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    # public -------------------------------------------------------------------------------------
    async def health(self, req):
        out = {"ok": True, "name": "baabaa", "version": __version__}
        if req.transport == "unix":  # the local terminal (`baabaa status`): where the server is and what it does
            q = self.app.gateway.queue.snapshot()
            out.update({
                "pid": os.getpid(), "urls": self.guard.urls(self.port, self.tls) if self.guard else [],
                "accounts": self.app.accounts.count(),
                "queue": {"running": {k: q["running"][k] for k in ("kind", "model", "label")} if q["running"] else None,
                          "waiting": len(q["waiting"]), "paused": q.get("paused")},
                "servers": {"llamacpp": (self.app.llamacpp.running() or {}).get("name"),
                            "sdcpp": (self.app.sdcpp.running() or {}).get("name")},
                "update": self.app.updates.status(), "restart": self.app.restarter.public(),
                "network": "local" if self.guard.local_only else "lan"})
        return json_response(out)

    async def ca_cert(self, req):
        crt = self.app.paths.tls / "ca.crt"
        if not crt.exists():
            raise HTTPError(404, "HTTPS is not enabled")
        return Response(crt.read_bytes(), content_type="application/x-x509-ca-cert",
                        headers=[("Content-Disposition", 'attachment; filename="baabaa-local-ca.crt"')])

    async def setup_info(self, req):
        return json_response({"needed": self.app.accounts.count() == 0})

    async def setup(self, req):
        if self.app.accounts.count() > 0:
            raise HTTPError(409, "Already set up")
        body = req.json()
        local = req.client_ip in ("127.0.0.1", "::1", "local")
        if not local and not (self.setup_token and secrets.compare_digest(body.get("token") or "", self.setup_token)):
            raise HTTPError(403, "Open the setup link printed by `baabaa serve`, or set up from this computer")
        account = self.app.accounts.create(body.get("name") or "owner", body.get("display_name") or "",
                                           body.get("password") or None, role="owner")
        return await self._open_session(req, account)

    async def profiles(self, req):
        if self.app.accounts.count() == 0:
            return json_response({"setup": True, "profiles": []})
        return json_response({"setup": False, "profiles": [
            {"id": a["id"], "name": a["name"], "display_name": a["display_name"], "color": a["color"],
             "has_password": a["has_password"]} for a in self.app.accounts.list() if not a["disabled"]]})

    async def login(self, req):
        body = req.json()
        account = self.app.accounts.get(body.get("account_id") or "") or self.app.accounts.by_name(body.get("name") or "")
        if account is None or account["disabled"]:
            raise HTTPError(401, "No such account")
        if req.transport == "unix" and not secrets.compare_digest(req.headers.get("x-baabaa-local-key") or "", self.app.local_key):
            raise HTTPError(403, "The terminal client must present the local key")
        if not self.app.accounts.check_password(account["id"], body.get("password"), req.client_ip):
            self.app.stats.event("login_failed", account["id"], window=req.window)
            raise HTTPError(401, "Wrong password")
        return await self._open_session(req, account)

    async def _open_session(self, req, account):
        client = "terminal" if req.transport == "unix" else "browser"
        token, csrf = self.app.accounts.open_session(account["id"], client, req.client_ip)
        self.app.stats.event("login", account["id"], window=client)
        body = {"account": account, "csrf": csrf}
        if req.transport == "unix":
            body["token"] = token
            return json_response(body)
        return json_response(body, headers=[("Set-Cookie", self._cookie(token, 30 * 86400))])

    async def logout(self, req):
        token = req.cookie(COOKIE) or (req.headers.get("authorization") or "")[7:]
        self.app.accounts.close_session(token)
        return json_response({"ok": True}, headers=[("Set-Cookie", self._cookie("", 0))])

    # me -----------------------------------------------------------------------------------------
    async def me(self, req):
        a = req.account
        return json_response({"account": a, "csrf": req.session.get("csrf"), "version": __version__,
                              "grants": self.app.accounts.grants(a["id"]),
                              "modes": [{"id": m, "label": permissions.MODE_LABELS[m]} for m in permissions.MODE_CYCLE]})

    async def update_me(self, req):
        body = req.json()
        fields = {}
        if "display_name" in body:
            fields["display_name"] = clip(str(body["display_name"]).strip() or req.account["name"], 60)
        if "color" in body and re.fullmatch(r"#[0-9a-fA-F]{6}", str(body["color"])):
            fields["color"] = body["color"]
        if fields:
            self.app.accounts.update(req.account["id"], **fields)
        if isinstance(body.get("settings"), dict):
            allowed = {"theme", "instructions", "default_mode", "default_model", "think", "send_key", "font", "code_wrap",
                       "show_thinking", "language", "voice", "reduce_motion", "density", "style", "memory", "chat_search",
                       "code_exec", "notify", "image_model", "recent_colors", "seen_version"}
            settings = {k: v for k, v in body["settings"].items() if k in allowed}
            if "recent_colors" in settings:  # the colour picker's own swatches: up to 15 colours
                colors = settings["recent_colors"] if isinstance(settings["recent_colors"], list) else []
                settings["recent_colors"] = [c.lower() for c in colors if isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c)][:15]
            self.app.accounts.update_settings(req.account["id"], settings)
        return json_response({"account": self.app.accounts.get(req.account["id"])})

    async def change_password(self, req):
        body = req.json()
        if req.account["has_password"] and not self.app.accounts.check_password(req.account["id"], body.get("current"), req.client_ip):
            raise HTTPError(401, "The current password is wrong")
        self.app.accounts.set_password(req.account["id"], body.get("new") or None)
        account = self.app.accounts.get(req.account["id"])
        return await self._open_session(req, account)

    async def list_keys(self, req):
        return json_response({"keys": self.app.accounts.api_keys(req.account["id"])})

    async def create_key(self, req):
        if req.session.get("api_key"):
            raise PermissionError("API keys cannot create more keys")
        key, token = self.app.accounts.create_api_key(req.account["id"], str(req.json().get("name") or "key"))
        self.app.stats.event("api_key_created", req.account["id"], window=req.window)
        return json_response({"key": key, "token": token})

    async def revoke_key(self, req):
        self.app.accounts.revoke_api_key(req.account["id"], req.params["kid"])
        return json_response({"ok": True})

    async def status(self, req):
        q = self.app.gateway.queue.snapshot()
        gpu = self.app.gpu.sample() if self.app.gpu.available else None
        return json_response({
            "version": __version__, "ollama": self.app.ollama_ok, "gpu": {"name": self.app.gpu.name, **(gpu or {}),
                                                                         "others": self.app.gpu_others()},
            "queue": {"busy": q["running"] is not None, "waiting": len(q["waiting"]), "paused": q.get("paused"),
                      "running": {"kind": q["running"]["kind"], "model": q["running"]["model"],
                                  "label": q["running"]["label"] if q["running"].get("account_id") == req.account["id"] else ""}
                      if q["running"] else None},
            "windows": self.app.events.windows(req.account["id"]),
            "network": "local" if self.guard.local_only else "lan"})

    async def events(self, req):
        sub = self.app.events.subscribe(req.account["id"], req.account["role"] == "owner", req.window)
        self.app.stats.event("window_open", req.account["id"], window=req.window)

        async def stream():
            try:
                yield sse_event("hello", {"version": __version__, "account_id": req.account["id"], "time": now_ms()})
                while True:
                    try:
                        ev = await sub.get(15)
                    except asyncio.TimeoutError:
                        yield b": keep-alive\n\n"
                        continue
                    if ev is None:
                        return
                    yield sse_event(ev["type"], ev["data"])
            finally:
                sub.close()
                self.app.stats.event("window_close", req.account["id"], window=req.window)

        return StreamResponse(stream())

    # conversations --------------------------------------------------------------------------------
    async def list_conversations(self, req):
        store = self._store(req)
        q = req.query
        convs = store.conversations(limit=min(200, int(q.get("limit") or 60)), before_ms=int(q["before"]) if q.get("before") else None,
                                    query=q.get("q") or None, archived=q.get("archived") == "1", folder_only=q.get("folders") == "1",
                                    project_id=q.get("project") or None)
        running = set(self.app.agent.turns)
        for c in convs:
            c["running"] = c["id"] in running
            c.pop("settings", None)
        return json_response({"conversations": convs})

    async def create_conversation(self, req):
        body = req.json()
        store = self._store(req)
        settings = req.account["settings"]
        folder = body.get("folder") or None
        if folder:
            folder = self._check_folder(req, folder)
        mode = body.get("mode") or settings.get("default_mode") or ("accept_edits" if folder else "manual")
        if mode not in permissions.MODES:
            raise ValueError("Unknown mode")
        model = body.get("model") or settings.get("default_model") or self.app.registry.default_chat()
        conv_settings = {}
        if "think" in body:
            conv_settings["think"] = body["think"]
        elif settings.get("think") not in (None, False):
            conv_settings["think"] = settings["think"]
        if body.get("network"):
            conv_settings["network"] = True
        project_id = body.get("project_id") or None
        if project_id and store.project(project_id) is None:
            raise KeyError(project_id)
        conv = store.create_conversation(title=clip(body.get("title") or "", 200), model=model, mode=mode, folder=folder,
                                         incognito=bool(body.get("incognito")) and not project_id, settings=conv_settings,
                                         project_id=project_id)
        self.app.stats.event("conversation_created", req.account["id"], conv["id"], req.window,
                             folder=bool(folder), incognito=conv["incognito"], model=model)
        return json_response({"conversation": conv})

    def _check_folder(self, req, folder: str) -> str:
        folder = os.path.realpath(os.path.expanduser(folder))
        if not os.path.isdir(folder):
            raise ValueError(f"Not a folder: {folder}")
        if self.app.accounts.folder_access(req.account, folder) is None:
            raise PermissionError("That folder has not been granted to this account")
        return folder

    async def get_conversation(self, req):
        store = self._store(req)
        conv = self._conv(req)
        thread = store.thread(conv["id"])
        live_id, live = self.app.agent.live_blocks(conv["id"])
        for m in thread:
            if m["id"] == live_id:
                m["blocks"] = live
        num_ctx = None
        if conv["model"]:
            m = self.app.registry.get(conv["model"])
            num_ctx = m["num_ctx"] if m else None
        used = next((m["meta"].get("context_used") for m in reversed(thread) if m["role"] == "assistant" and m["meta"].get("context_used")), None)
        return json_response({
            "conversation": conv, "thread": thread, "state": self.app.agent.state(conv["id"]),
            "artifacts": store.artifacts(conv["id"]), "context": {"used": used, "num_ctx": num_ctx},
            "trusted": self.app.maindb.is_trusted(conv["folder"]) if conv["folder"] else None,
            "shells": self.app.shells.list_background(conv["id"]),
            "project": ({k: v for k, v in (store.project(conv["project_id"]) or {}).items() if k in ("id", "name")} or None)
            if conv.get("project_id") else None,
        })

    async def update_conversation(self, req):
        store = self._store(req)
        conv = self._conv(req)
        body = req.json()
        fields = {}
        if "title" in body:
            fields["title"] = clip(str(body["title"]).strip(), 200)
        for k in ("pinned", "archived"):
            if k in body:
                fields[k] = bool(body[k])
        if "mode" in body:
            if body["mode"] not in permissions.MODES:
                raise ValueError("Unknown mode")
            fields["mode"] = body["mode"]
        if "model" in body:
            names = [m["name"] for m in self.app.registry.approved("chat")]
            if body["model"] not in names:
                raise ModelNotAllowed("That model is not approved for use")
            fields["model"] = body["model"]
            self.app.stats.event("model_switched", req.account["id"], conv["id"], req.window, frm=conv["model"], to=body["model"])
        if "folder" in body:
            fields["folder"] = self._check_folder(req, body["folder"]) if body["folder"] else None
        if "project_id" in body:
            if body["project_id"] and store.project(body["project_id"]) is None:
                raise KeyError(body["project_id"])
            if conv["incognito"]:
                raise ValueError("Incognito conversations cannot join a project")
            fields["project_id"] = body["project_id"] or None
        if fields:
            conv = store.update_conversation(conv["id"], **fields)
        if isinstance(body.get("settings"), dict):
            patch = {k: v for k, v in body["settings"].items() if k in ("think", "network", "style")}
            conv = store.patch_settings(conv["id"], patch)
        self.app.events.publish(req.account["id"], "conv", {"conversation": conv})
        return json_response({"conversation": conv})

    def _scratch(self, account_id: str, cid: str) -> str:
        return str(self.app.paths.account(account_id) / "files" / "scratch" / cid)

    async def conv_files(self, req):
        """The conversation's files: its working folder, or the chat's scratch folder. `path` downloads one."""
        conv = self._conv(req)
        if conv["folder"]:
            if self.app.accounts.folder_access(req.account, conv["folder"]) is None:
                raise PermissionError("That folder is not available to this account")
            root = conv["folder"]
        else:
            root = self._scratch(req.account["id"], conv["id"])
        rel = req.query.get("path") or ""
        if not rel:
            if conv["folder"]:
                raise ValueError("Give the path of a file")
            from ..agent.loop import _snapshot
            files = [{"path": p, "size": v[0], "mtime_ms": v[1] // 1_000_000}
                     for p, v in sorted(_snapshot(root).items())] if os.path.isdir(root) else []
            return json_response({"files": files})
        real_root = os.path.realpath(root)
        full = os.path.realpath(os.path.join(real_root, rel))
        if not full.startswith(real_root + os.sep) or not os.path.isfile(full):
            raise KeyError(rel)
        size = os.path.getsize(full)
        if size > 512 * 2**20:
            raise ValueError("That file is too large to download here")
        with open(full, "rb") as f:
            data = f.read()
        name = re.sub(r'[\x00-\x1f"\\/]', "_", os.path.basename(full))
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        inline = req.query.get("inline") == "1" and ctype in ("image/png", "image/jpeg", "image/gif", "image/webp")
        self.app.stats.event("file_downloaded", req.account["id"], conv["id"], req.window, bytes=size,
                             ext=os.path.splitext(name)[1].lower())
        return Response(data, content_type=ctype if inline else "application/octet-stream",
                        headers=[("Content-Disposition", f'{"inline" if inline else "attachment"}; filename="{name}"')])

    async def delete_conversation(self, req):
        conv = self._conv(req)
        self.app.agent.stop(conv["id"])
        self._store(req).delete_conversation(conv["id"])
        import shutil
        shutil.rmtree(self._scratch(req.account["id"], conv["id"]), ignore_errors=True)
        self.app.events.publish(req.account["id"], "conv.deleted", {"conv_id": conv["id"]})
        self.app.stats.event("conversation_deleted", req.account["id"], conv["id"], req.window)
        return json_response({"ok": True})

    async def send_message(self, req):
        conv = self._conv(req)
        body = req.json()
        res = await self.app.agent.send(req.account, conv["id"], str(body.get("text") or ""), body.get("attachments") or [],
                                        req.window, parent_id=body.get("parent_id"), edit=bool(body.get("edit")),
                                        research=bool(body.get("research")),
                                        image=body["image"] if isinstance(body.get("image"), dict) else None)
        return json_response(res)

    async def stop(self, req):
        conv = self._conv(req)
        stopped = self.app.agent.stop(conv["id"])
        if stopped:
            self.app.stats.event("stop", req.account["id"], conv["id"], req.window)
        return json_response({"stopped": stopped})

    async def regenerate(self, req):
        conv = self._conv(req)
        self.app.agent.regenerate(req.account, conv["id"], req.json().get("message_id") or "", req.window)
        return json_response({"ok": True})

    async def branch(self, req):
        store = self._store(req)
        conv = self._conv(req)
        mid = req.json().get("message_id") or ""
        m = store.message(mid)
        if m is None or m["conv_id"] != conv["id"]:
            raise KeyError(mid)
        if conv["id"] in self.app.agent.turns:
            raise RuntimeError("Stop the running reply first")
        conv = store.update_conversation(conv["id"], leaf_id=store.descend(mid))
        self.app.stats.event("branch_switched", req.account["id"], conv["id"], req.window)
        return json_response({"conversation": conv, "thread": store.thread(conv["id"])})

    async def compact(self, req):
        conv = self._conv(req)
        if conv["id"] in self.app.agent.turns:
            raise RuntimeError("Stop the running reply first")
        node = await self.app.agent.compact(req.account, conv["id"], window=req.window)
        return json_response({"message": node})

    async def rewind(self, req):
        conv = self._conv(req)
        body = req.json()
        res = await self.app.agent.rewind(req.account, conv["id"], body.get("message_id") or "",
                                          bool(body.get("files")), body.get("conversation", True) is not False)
        return json_response(res)

    async def fork(self, req):
        store = self._store(req)
        conv = self._conv(req)
        mid = req.json().get("message_id") or conv["leaf_id"]
        new = store.copy_thread(conv["id"], mid, clip((conv["title"] or "Conversation") + " (fork)", 200))
        self.app.stats.event("fork", req.account["id"], conv["id"], req.window, new=new["id"])
        return json_response({"conversation": new})

    async def feedback(self, req):
        """Thumbs up or down on a reply: kept with the message and in the statistics, never sent anywhere."""
        store = self._store(req)
        conv = self._conv(req)
        body = req.json()
        m = store.message(body.get("message_id") or "")
        if m is None or m["conv_id"] != conv["id"] or m["role"] != "assistant":
            raise KeyError("message")
        rating = int(body.get("rating") or 0)
        if rating not in (-1, 0, 1):
            raise ValueError("rating is -1, 0 or 1")
        meta = {**m["meta"], "feedback": rating}
        if body.get("note"):
            meta["feedback_note"] = clip(str(body["note"]), 2000)
        store.update_message(m["id"], meta=meta)
        self.app.stats.event("feedback", req.account["id"], conv["id"], req.window, rating=rating, model=m["model"],
                             with_note=bool(body.get("note")))
        return json_response({"ok": True})

    async def export(self, req):
        from ..export import export_conversation
        store = self._store(req)
        conv = self._conv(req)
        fmt = req.query.get("format", "md")
        body, ctype, ext = export_conversation(store, conv, fmt, req.query.get("scope", "branch"))
        name = re.sub(r"[^\w.-]+", "-", conv["title"] or "conversation").strip("-")[:60] or "conversation"
        self.app.stats.event("export", req.account["id"], conv["id"], req.window, format=fmt)
        return Response(body, content_type=ctype, headers=[("Content-Disposition", f'attachment; filename="{name}.{ext}"')])

    async def shells(self, req):
        conv = self._conv(req)
        return json_response({"shells": self.app.shells.list_background(conv["id"])})

    # projects -------------------------------------------------------------------------------------
    def _project(self, req) -> dict:
        p = self._store(req).project(req.params["pid"])
        if p is None:
            raise KeyError(req.params["pid"])
        return p

    async def list_projects(self, req):
        return json_response({"projects": self._store(req).projects(archived=req.query.get("archived") == "1")})

    async def create_project(self, req):
        body = req.json()
        name = clip(str(body.get("name") or "").strip(), 120)
        if not name:
            raise ValueError("Give the project a name")
        p = self._store(req).create_project(name, clip(str(body.get("description") or ""), 2000),
                                            clip(str(body.get("instructions") or ""), 20000))
        self.app.stats.event("project_created", req.account["id"], window=req.window)
        self.app.events.publish(req.account["id"], "project", {"project": p})
        return json_response({"project": p})

    async def get_project(self, req):
        store = self._store(req)
        p = self._project(req)
        files = store.project_files(p["id"])
        for f in files:
            f.pop("path", None)
        convs = store.conversations(limit=200, project_id=p["id"])
        for c in convs:
            c.pop("settings", None)
        num_ctx = None
        model = self.app.registry.default_chat()
        if model:
            num_ctx = self.app.registry.get(model)["num_ctx"]
        _, mode = self.app.knowledge.prompt(req.account["id"], p, num_ctx or 32768)
        return json_response({"project": p, "files": files, "conversations": convs, "knowledge_mode": mode,
                              "embed_model": self.app.registry.embed_model()})

    async def update_project(self, req):
        p = self._project(req)
        body = req.json()
        fields = {}
        for key, limit in (("name", 120), ("description", 2000), ("instructions", 20000)):
            if key in body:
                fields[key] = clip(str(body[key] or "").strip() if key == "name" else str(body[key] or ""), limit)
        if fields.get("name") == "":
            raise ValueError("A project needs a name")
        if "archived" in body:
            fields["archived"] = bool(body["archived"])
        p = self._store(req).update_project(p["id"], **fields) if fields else p
        self.app.events.publish(req.account["id"], "project", {"project": p})
        return json_response({"project": p})

    async def delete_project(self, req):
        p = self._project(req)
        self.app.knowledge.delete_project(req.account["id"], p["id"])
        self.app.stats.event("project_deleted", req.account["id"], window=req.window)
        self.app.events.publish(req.account["id"], "project", {"deleted": p["id"]})
        return json_response({"ok": True})

    async def add_project_file(self, req):
        p = self._project(req)
        name = re.sub(r"[\x00-\x1f/\\]", "_", os.path.basename(req.query.get("name") or "file").strip() or "file")[:200]
        if not req.body:
            raise ValueError("Empty upload")
        mime = (req.headers.get("content-type") or mimetypes.guess_type(name)[0] or "application/octet-stream").split(";")[0]
        f = await self.app.knowledge.add_file(req.account["id"], p["id"], name, mime, req.body)
        f.pop("path", None)
        self.app.stats.event("project_file_added", req.account["id"], window=req.window, kind=f["kind"], mime=mime,
                             bytes=len(req.body), chars=f.get("chars"))
        return json_response({"file": f})

    async def add_project_text(self, req):
        p = self._project(req)
        body = req.json()
        f = self.app.knowledge.add_text(req.account["id"], p["id"], clip(str(body.get("name") or "Text"), 200),
                                        str(body.get("text") or "")[:2_000_000])
        f.pop("path", None)
        self.app.stats.event("project_file_added", req.account["id"], window=req.window, kind="text", chars=f.get("chars"))
        return json_response({"file": f})

    async def get_project_file(self, req):
        p = self._project(req)
        f = self._store(req).project_file(req.params["fid"])
        if f is None or f["project_id"] != p["id"]:
            raise KeyError(req.params["fid"])
        if req.query.get("download") == "1" and f.get("path"):
            with open(f["path"], "rb") as fh:
                data = fh.read()
            return Response(data, content_type="application/octet-stream",
                            headers=[("Content-Disposition", f'attachment; filename="{f["name"]}"')])
        f.pop("path", None)
        f["text"] = clip(f["text"], 200_000)
        return json_response({"file": f})

    async def delete_project_file(self, req):
        p = self._project(req)
        f = self._store(req).project_file(req.params["fid"], with_text=False)
        if f is None or f["project_id"] != p["id"]:
            raise KeyError(req.params["fid"])
        self.app.knowledge.delete_file(req.account["id"], f["id"])
        self.app.events.publish(req.account["id"], "project.file", {"project_id": p["id"], "deleted": f["id"]})
        return json_response({"ok": True})

    async def search_project(self, req):
        p = self._project(req)
        res = await self.app.knowledge.search(req.account["id"], p["id"], req.query.get("q") or "",
                                              k=min(20, int(req.query.get("k") or 6)), window=req.window)
        return json_response(res)

    # git worktrees --------------------------------------------------------------------------------
    async def _git(self, *args, cwd: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec("git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        out, _ = await asyncio.wait_for(proc.communicate(), 60)
        return proc.returncode, out.decode(errors="replace").strip()

    async def add_worktree(self, req):
        """Move the conversation to a new git worktree of its folder's repository, on a branch of its own."""
        conv = self._conv(req)
        if not conv["folder"]:
            raise ValueError("The conversation has no working folder")
        if self.app.accounts.folder_access(req.account, conv["folder"]) != "rw":
            raise PermissionError("This account cannot change that folder")
        code, top = await self._git("rev-parse", "--show-toplevel", cwd=conv["folder"])
        if code != 0:
            raise ValueError("The working folder is not in a git repository")
        name = re.sub(r"[^a-z0-9]+", "-", str(req.json().get("name") or conv["title"] or "work").lower()).strip("-")[:40] or "work"
        slug = f"{name}-{conv['id'][-5:]}"
        path = self.app.paths.account(req.account["id"]) / "worktrees" / f"{os.path.basename(top)}-{slug}"
        path.parent.mkdir(parents=True, exist_ok=True)
        branch = f"baabaa/{slug}"
        code, out = await self._git("worktree", "add", "-b", branch, str(path), "HEAD", cwd=top)
        if code != 0:
            raise RuntimeError(f"git worktree add failed: {clip(out, 400)}")
        sub = os.path.relpath(conv["folder"], top)
        folder = os.path.realpath(os.path.join(path, sub))
        if self.app.maindb.is_trusted(conv["folder"]):
            self.app.maindb.trust(folder, req.account["id"])  # the same code as the trusted folder
        store = self._store(req)
        original = conv["folder"]
        conv = store.update_conversation(conv["id"], folder=folder)
        conv = store.patch_settings(conv["id"], {"worktree": {"path": str(path), "branch": branch, "repo": top,
                                                              "from": original}})
        self.app.stats.event("worktree_created", req.account["id"], conv["id"], req.window)
        self.app.events.publish(req.account["id"], "conv", {"conversation": conv})
        return json_response({"conversation": conv, "branch": branch, "path": str(path)})

    async def remove_worktree(self, req):
        conv = self._conv(req)
        wt = conv["settings"].get("worktree")
        if not wt:
            raise ValueError("The conversation is not in a worktree")
        if conv["id"] in self.app.agent.turns:
            raise RuntimeError("Stop the running reply first")
        force = req.query.get("force") == "1"
        code, out = await self._git("worktree", "remove", *(["--force"] if force else []), wt["path"], cwd=wt["repo"])
        if code != 0:
            raise RuntimeError("The worktree has changes that are not committed. Commit them, or remove it anyway."
                               if "contains modified or untracked files" in out or "is dirty" in out else clip(out, 400))
        store = self._store(req)
        conv = store.update_conversation(conv["id"], folder=wt.get("from") or wt["repo"])
        conv = store.patch_settings(conv["id"], {"worktree": None})
        self.app.events.publish(req.account["id"], "conv", {"conversation": conv})
        return json_response({"conversation": conv, "branch": wt["branch"],
                              "note": f"The branch {wt['branch']} stays in the repository."})

    # sharing between accounts ---------------------------------------------------------------------
    async def share_conversation(self, req):
        """A read-only copy of the conversation's current branch, for chosen accounts or everyone here."""
        conv = self._conv(req)
        if conv["incognito"]:
            raise ValueError("Incognito conversations cannot be shared")
        body = req.json()
        to = body.get("to")
        ids = {a["id"] for a in self.app.accounts.list()} - {req.account["id"]}
        if to == "*":
            recipients = ["*"]
        else:
            recipients = [x for x in (to or []) if x in ids]
            if not recipients:
                raise ValueError("Choose who to share it with")
        store = self._store(req)
        keep = ("type", "text", "name", "args", "status", "output", "files", "sources", "research", "auto")
        thread = []
        for m in store.thread(conv["id"]):
            blocks = [{k: v for k, v in b.items() if k in keep} for b in m["blocks"]
                      if b.get("type") in ("text", "tool", "notice", "attachment")]
            for b in blocks:  # attachments travel as names only; tool output is kept short
                if b.get("type") == "tool" and b.get("output"):
                    b["output"] = clip(b["output"], 4000)
                b.pop("files", None)
            thread.append({"id": m["id"], "role": m["role"], "blocks": blocks, "model": m["model"],
                           "created_ms": m["created_ms"], "status": m["status"], "meta": {}})
        artifacts = [store.artifact(a["id"]) for a in store.artifacts(conv["id"])]
        snapshot = {"title": conv["title"], "thread": thread, "artifacts": [
            {k: a[k] for k in ("id", "title", "kind", "language", "content", "version")} for a in artifacts if a]}
        share_id = new_id()
        self.app.maindb.share_create(share_id, req.account["id"], recipients, conv["id"], conv["title"] or "Untitled", snapshot)
        self.app.stats.event("share_created", req.account["id"], conv["id"], req.window, recipients=len(recipients),
                             everyone=recipients == ["*"], messages=len(thread))
        for aid in (ids if recipients == ["*"] else recipients):
            self.app.events.publish(aid, "share", {"from": req.account["display_name"], "title": conv["title"]})
        return json_response({"share": {"id": share_id, "recipients": recipients}})

    def _share_public(self, r: dict) -> dict:
        acc = self.app.accounts.get(r["from_account"]) or {}
        names = []
        for x in r["recipients"]:
            if x == "*":
                names.append("everyone")
            else:
                a = self.app.accounts.get(x)
                names.append(a["display_name"] if a else "?")
        return {"id": r["id"], "title": r["title"], "created_ms": r["created_ms"], "from": acc.get("display_name"),
                "from_color": acc.get("color"), "to": names}

    async def list_shares(self, req):
        theirs, mine = self.app.maindb.shares_for(req.account["id"])
        return json_response({"shared_with_me": [self._share_public(r) for r in theirs],
                              "shared_by_me": [self._share_public(r) for r in mine]})

    def _share(self, req) -> dict:
        r = self.app.maindb.share_get(req.params["shid"])
        aid = req.account["id"]
        if r is None or not (r["from_account"] == aid or "*" in r["recipients"] or aid in r["recipients"]):
            raise KeyError(req.params["shid"])
        return r

    async def get_share(self, req):
        r = self._share(req)
        return json_response({"share": {**self._share_public(r), "snapshot": r["snapshot"],
                                        "mine": r["from_account"] == req.account["id"]}})

    async def copy_share(self, req):
        """Continue a shared conversation: a copy in this account's own history."""
        r = self._share(req)
        store = self._store(req)
        snap = r["snapshot"]
        conv = store.create_conversation(title=clip(f"{snap.get('title') or 'Shared'} (shared)", 200),
                                         model=self.app.registry.default_chat())
        parent = None
        for m in snap.get("thread") or []:
            if m["role"] not in ("user", "assistant", "compaction"):
                continue
            blocks = [b for b in m["blocks"] if b.get("type") != "attachment"] or [{"type": "text", "text": ""}]
            parent = store.add_message(conv["id"], parent, m["role"], blocks, m.get("model"), "ok")["id"]
        for a in snap.get("artifacts") or []:
            store.create_artifact(conv["id"], None, a["title"], a["kind"], a["content"], a.get("language"))
        self.app.stats.event("share_copied", req.account["id"], conv["id"], req.window)
        return json_response({"conversation": store.conversation(conv["id"])})

    async def delete_share(self, req):
        r = self._share(req)
        if r["from_account"] != req.account["id"]:
            raise PermissionError("Only the person who shared it can stop sharing")
        self.app.maindb.share_delete(r["id"])
        return json_response({"ok": True})

    # scheduled tasks ------------------------------------------------------------------------------
    def _schedule_public(self, sch: dict) -> dict:
        from ..scheduler import describe
        return {**sch, "when": describe(sch["spec"])}

    def _schedule_body(self, body: dict, store) -> tuple[dict, dict]:
        from ..scheduler import validate
        spec = validate(body.get("spec") or {})
        st = body.get("settings") or {}
        settings = {"research": bool(st.get("research")), "same_conversation": bool(st.get("same_conversation")),
                    "think": bool(st.get("think"))}
        if st.get("model"):
            settings["model"] = str(st["model"])
        if st.get("project_id"):
            if store.project(st["project_id"]) is None:
                raise KeyError(st["project_id"])
            settings["project_id"] = st["project_id"]
        return spec, settings

    async def list_schedules(self, req):
        return json_response({"schedules": [self._schedule_public(x) for x in self._store(req).schedules()],
                              "timezone": time.strftime("%Z"), "now_ms": now_ms()})

    async def create_schedule(self, req):
        from ..scheduler import next_run
        body = req.json()
        store = self._store(req)
        title = clip(str(body.get("title") or "").strip(), 120)
        prompt = str(body.get("prompt") or "").strip()
        if not title or not prompt:
            raise ValueError("A scheduled task needs a title and a prompt")
        spec, settings = self._schedule_body(body, store)
        nxt = next_run(spec, now_ms())
        if nxt is None:
            raise ValueError("That time has already passed")
        sch = store.add_schedule(title, clip(prompt, 20000), spec, settings, nxt)
        self.app.stats.event("schedule_created", req.account["id"], window=req.window, kind=spec["kind"])
        return json_response({"schedule": self._schedule_public(sch)})

    async def update_schedule(self, req):
        from ..scheduler import next_run
        store = self._store(req)
        sch = store.schedule(req.params["sid"])
        if sch is None:
            raise KeyError(req.params["sid"])
        body = req.json()
        fields = {}
        if "title" in body:
            fields["title"] = clip(str(body["title"]).strip(), 120) or sch["title"]
        if "prompt" in body:
            fields["prompt"] = clip(str(body["prompt"]).strip(), 20000) or sch["prompt"]
        if "spec" in body or "settings" in body:
            spec, settings = self._schedule_body({"spec": body.get("spec", sch["spec"]),
                                                  "settings": body.get("settings", sch["settings"])}, store)
            fields.update(spec=spec, settings=settings, next_ms=next_run(spec, now_ms()))
        if "enabled" in body:
            fields["enabled"] = bool(body["enabled"])
            if fields["enabled"]:
                fields["next_ms"] = next_run(fields.get("spec", sch["spec"]), now_ms())
                if fields["next_ms"] is None:
                    raise ValueError("That time has already passed; change the schedule first")
        sch = store.update_schedule(sch["id"], **fields)
        return json_response({"schedule": self._schedule_public(sch)})

    async def delete_schedule(self, req):
        store = self._store(req)
        if store.schedule(req.params["sid"]) is None:
            raise KeyError(req.params["sid"])
        store.delete_schedule(req.params["sid"])
        return json_response({"ok": True})

    async def run_schedule(self, req):
        store = self._store(req)
        sch = store.schedule(req.params["sid"])
        if sch is None:
            raise KeyError(req.params["sid"])
        sch = await self.app.scheduler.run(req.account, sch, manual=True)
        return json_response({"schedule": self._schedule_public(sch)})

    # memory ---------------------------------------------------------------------------------------
    async def list_memory(self, req):
        store = self._store(req)
        names = {p["id"]: p["name"] for p in store.projects()}
        items = store.memories(scope="all")
        for m in items:
            m["project_name"] = names.get(m["project_id"]) if m["project_id"] else None
        s = req.account.get("settings") or {}
        return json_response({"memories": items, "enabled": s.get("memory", True) is not False,
                              "chat_search": s.get("chat_search", True) is not False})

    async def add_memory(self, req):
        body = req.json()
        text = " ".join(str(body.get("text") or "").split())
        if not text:
            raise ValueError("The memory is empty")
        store = self._store(req)
        if body.get("project_id") and store.project(body["project_id"]) is None:
            raise KeyError(body["project_id"])
        m = store.add_memory(clip(text, 500), body.get("project_id") or None, "user")
        self.app.stats.event("memory_add", req.account["id"], window=req.window, by="user")
        self.app.events.publish(req.account["id"], "memory", {})
        return json_response({"memory": m})

    async def update_memory(self, req):
        store = self._store(req)
        if store.memory(req.params["mid"]) is None:
            raise KeyError(req.params["mid"])
        text = " ".join(str(req.json().get("text") or "").split())
        if not text:
            raise ValueError("The memory is empty")
        m = store.update_memory(req.params["mid"], clip(text, 500))
        self.app.events.publish(req.account["id"], "memory", {})
        return json_response({"memory": m})

    async def delete_memory(self, req):
        store = self._store(req)
        if store.memory(req.params["mid"]) is None:
            raise KeyError(req.params["mid"])
        store.delete_memory(req.params["mid"])
        self.app.stats.event("memory_delete", req.account["id"], window=req.window, by="user")
        self.app.events.publish(req.account["id"], "memory", {})
        return json_response({"ok": True})

    async def search(self, req):
        q = (req.query.get("q") or "").strip()
        if not q:
            return json_response({"results": []})
        self.app.stats.event("search", req.account["id"], window=req.window, terms=len(q.split()))
        return json_response({"results": self._store(req).search(q)})

    async def answer_pending(self, req):
        body = req.json()
        allowed = {"decision", "reason", "rule", "text", "mode"}
        self.app.agent.resolve(req.params["pid"], req.account, {k: v for k, v in body.items() if k in allowed})
        return json_response({"ok": True})

    # artifacts and files --------------------------------------------------------------------------
    async def list_artifacts(self, req):
        return json_response({"artifacts": self._store(req).artifacts(req.query.get("conv_id") or None)})

    async def get_artifact(self, req):
        v = int(req.query["version"]) if req.query.get("version") else None
        a = self._store(req).artifact(req.params["aid"], v)
        if a is None:
            raise KeyError(req.params["aid"])
        return json_response({"artifact": a})

    async def list_images(self, req):
        before = int(req.query["before"]) if req.query.get("before") else None
        return json_response({"images": self._store(req).generated_images(min(500, int(req.query.get("limit") or 200)), before)})

    async def export_artifact(self, req):
        """A document or slide artifact as Word, PDF or PowerPoint (written by baabaa's own writers)."""
        from .. import writers
        v = int(req.query["version"]) if req.query.get("version") else None
        a = self._store(req).artifact(req.params["aid"], v)
        if a is None:
            raise KeyError(req.params["aid"])
        fmt = req.query.get("format") or "docx"
        allowed = {"markdown": ("docx", "pdf", "pptx"), "slides": ("pptx", "pdf", "docx")}.get(a["kind"], ())
        if fmt not in allowed:
            raise ValueError(f"A {a['kind']} artifact cannot be exported as {fmt}")
        data = await asyncio.to_thread(writers.DOCUMENTS["." + fmt], a["content"], a["title"])
        name = (re.sub(r"[^\w.-]+", "-", a["title"]).strip("-") or "artifact")[:80] + "." + fmt
        self.app.stats.event("export", req.account["id"], a["conv_id"], req.window, format=fmt, artifact=True)
        return Response(data, content_type="application/octet-stream",
                        headers=[("Content-Disposition", f'attachment; filename="{name}"')])

    async def artifact_frame(self, req):
        v = int(req.query["v"]) if req.query.get("v") else None
        a = self._store(req).artifact(req.params["aid"], v)
        if a is None:
            raise KeyError(req.params["aid"])
        from ..export import artifact_document
        doc = artifact_document(a)
        return Response(doc, content_type="text/html; charset=utf-8",
                        headers=[("Content-Security-Policy", ARTIFACT_CSP), ("Cache-Control", "no-store")])

    async def upload(self, req):
        name = os.path.basename(req.query.get("name") or "file").strip() or "file"
        name = re.sub(r"[\x00-\x1f/\\]", "_", name)[:200]
        data = req.body
        if not data:
            raise ValueError("Empty upload")
        mime = (req.headers.get("content-type") or mimetypes.guess_type(name)[0] or "application/octet-stream").split(";")[0]
        kind = docs.classify(name, mime, data)
        aid = new_id()
        folder = self.app.paths.account_files(req.account["id"], "uploads", aid)
        path = folder / name
        with open(path, "wb") as f:
            f.write(data)
        text = None
        if kind in ("text", "document"):
            text = await asyncio.to_thread(docs.extract, name, data)
        store = self._store(req)
        att = store.add_attachment(name, mime, len(data), kind, path, text, {"chars": len(text or "")},
                                   conv_id=req.query.get("conv_id") or None)
        self.app.stats.event("upload", req.account["id"], req.query.get("conv_id"), req.window, kind=kind, mime=mime,
                             bytes=len(data), extracted_chars=len(text or ""))
        att.pop("path", None)
        att["preview"] = clip(text or "", 400) if text else None
        att["text"] = None
        return json_response({"attachment": att})

    async def transcribe(self, req):
        """Dictation: speech (WAV or MP3) to text with an approved audio-capable model, on the GPU like any
        other request. The audio is not kept."""
        import base64
        data = req.body or b""
        if len(data) < 1000:
            raise ValueError("No audio was recorded")
        if len(data) > 25 * 2**20:
            raise ValueError("The recording is too long (25 MB at most)")
        if not (data[:4] == b"RIFF" and data[8:12] == b"WAVE") and not (data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3")):
            raise ValueError("Send the recording as WAV or MP3")
        model = self.app.registry.audio_model()
        if not model:
            raise ModelNotAllowed("No approved model can hear audio yet (for example gemma4). The owner approves one in Settings > Models.")
        lang = (req.query.get("lang") or "").strip()[:20]
        caps = (self.app.registry.get(model) or {}).get("info", {}).get("capabilities") or []
        prompt = ("Transcribe the speech in this recording exactly as spoken" + (f" (the language is {lang})" if lang else "")
                  + ". Write only the words, with normal punctuation, and nothing else.")
        t0 = time.monotonic()
        res = await self.app.gateway.complete(
            model=model, kind="transcribe", messages=[{"role": "user", "content": prompt, "images": [base64.b64encode(data).decode()]}],
            think=False if "thinking" in caps else None, options={"temperature": 0, "num_predict": 2000},
            account_id=req.account["id"], window=req.window, label="transcribing")
        text = res["content"].strip()
        self.app.stats.event("dictation", req.account["id"], window=req.window, bytes=len(data), chars=len(text),
                             seconds=round(time.monotonic() - t0, 2), model=model)
        return json_response({"text": text, "model": model})

    async def get_attachment(self, req):
        att = self._store(req).attachment(req.params["aid"])
        if att is None:
            raise KeyError(req.params["aid"])
        if req.query.get("text") == "1":
            return json_response({"text": att.get("text") or ""})
        with open(att["path"], "rb") as f:
            data = f.read()
        # inline only for a picture whose type is a plain raster image: the stored type came from the uploader
        inline = att["kind"] == "image" and att["mime"] in INLINE_IMAGES
        ctype = att["mime"] if inline else "application/octet-stream"
        name = re.sub(r'["\\\x00-\x1f]', "_", att["name"])
        return Response(data, content_type=ctype, headers=[("Content-Disposition", f'{"inline" if inline else "attachment"}; filename="{name}"')])

    async def browse_folders(self, req):
        path = req.query.get("path") or ""
        account = req.account
        if not path:
            roots = [g["path"] for g in self.app.accounts.grants(account["id"])]
            if account["role"] == "owner":
                roots = [os.path.expanduser("~")] + roots
            return json_response({"path": "", "parent": None, "folders": [{"name": r, "path": r} for r in roots]})
        path = os.path.realpath(os.path.expanduser(path))
        if self.app.accounts.folder_access(account, path) is None:
            raise PermissionError("That folder has not been granted to this account")
        try:
            names = sorted(e.name for e in os.scandir(path) if e.is_dir() and not e.name.startswith("."))
        except OSError as exc:
            raise ValueError(f"Cannot list {path}: {exc.strerror}") from exc
        parent = os.path.dirname(path)
        if self.app.accounts.folder_access(account, parent) is None or parent == path:
            parent = None
        return json_response({"path": path, "parent": parent, "trusted": self.app.maindb.is_trusted(path),
                              "folders": [{"name": n, "path": os.path.join(path, n)} for n in names[:500]]})

    async def trust_folder(self, req):
        path = self._check_folder(req, req.json().get("path") or "")
        self.app.maindb.trust(path, req.account["id"])
        return json_response({"trusted": True, "path": path})

    # rules ----------------------------------------------------------------------------------------
    async def get_rules(self, req):
        store = self._store(req)
        return json_response({"rules": store.rules(), "auto_rules": store.auto_rules()})

    async def add_rule(self, req):
        body = req.json()
        store = self._store(req)
        kind = body.get("kind")
        if kind in ("allow", "ask", "deny"):
            permissions.parse_rule(body.get("pattern") or "")
            rule = store.add_rule(kind, body["pattern"])
        elif kind in ("trust", "block", "exception"):
            if not (body.get("text") or "").strip():
                raise ValueError("Write the rule as a sentence")
            rule = store.add_auto_rule(kind, body["text"])
        else:
            raise ValueError("Unknown kind of rule")
        self.app.events.publish(req.account["id"], "rules", {})
        return json_response({"rule": rule})

    async def delete_rule(self, req):
        store = self._store(req)
        store.delete_rule(req.params["rid"])
        store.delete_auto_rule(req.params["rid"])
        self.app.events.publish(req.account["id"], "rules", {})
        return json_response({"ok": True})

    # customization --------------------------------------------------------------------------------
    EXT_KINDS = ("skills", "commands", "agents", "styles")

    def _ext_path(self, req, kind: str, name: str) -> str:
        if kind not in self.EXT_KINDS and kind != "hooks":
            raise ValueError("kind is skills, commands, agents, styles or hooks")
        base = self.app.paths.account_files(req.account["id"], "extend")
        if kind == "hooks":
            return os.path.join(str(base), "hooks.json")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) or ".." in name:
            raise ValueError("Names use letters, digits, '.', '_' or '-'")
        if kind == "skills":
            return os.path.join(str(base), "skills", name, "SKILL.md")
        return os.path.join(str(base), kind, name + ".md")

    async def extend_list(self, req):
        ext = self.app.extend
        folder, trusted = None, False
        if req.query.get("conv_id"):
            conv = self._store(req).conversation(req.query["conv_id"])
            if conv and conv["folder"]:
                folder, trusted = conv["folder"], self.app.maindb.is_trusted(conv["folder"])
        aid = req.account["id"]
        pick = lambda d: [{"name": v["name"], "description": v.get("description", ""), "source": v.get("source")} for v in d.values()]
        hooks = ext.hooks(aid, folder, trusted)
        return json_response({
            "skills": pick(ext.skills(aid, folder, trusted)), "commands": pick(ext.commands(aid, folder, trusted)),
            "agents": pick(ext.agents(aid, folder, trusted)),
            "styles": [{"name": v["name"], "label": v["label"], "source": v["source"]} for v in ext.styles(aid, folder, trusted).values()],
            "hooks": {k: len(v) for k, v in hooks.items()},
            "folder": folder, "folder_trusted": trusted})

    async def extend_get(self, req):
        path = self._ext_path(req, req.params["kind"], req.params["name"])
        try:
            with open(path, encoding="utf-8") as f:
                return json_response({"content": f.read()})
        except FileNotFoundError:
            return json_response({"content": ""})

    async def extend_put(self, req):
        kind = req.params["kind"]
        path = self._ext_path(req, kind, req.params["name"])
        content = str(req.json().get("content") or "")
        if len(content) > 200_000:
            raise ValueError("Too long")
        if kind == "hooks":
            try:
                data = json.loads(content or "{}")
            except ValueError as exc:
                raise ValueError(f"Hooks must be JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError('Hooks JSON is an object like {"hooks": {"PreToolUse": [...]}}')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self.app.stats.event("customization_saved", req.account["id"], window=req.window, kind=kind)
        return json_response({"ok": True})

    async def extend_delete(self, req):
        import shutil
        kind = req.params["kind"]
        path = self._ext_path(req, kind, req.params["name"])
        if kind == "skills":
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        return json_response({"ok": True})

    # connectors -----------------------------------------------------------------------------------
    async def connectors(self, req):
        out = []
        for c in self.app.mcp.configs(req.account["id"]):
            live = self.app.mcp.live.get((req.account["id"], c["name"]))
            pub = {k: v for k, v in c.items() if k not in ("env", "headers")}
            pub["env_keys"] = sorted((c.get("env") or {}).keys())
            pub["header_keys"] = sorted((c.get("headers") or {}).keys())
            pub["connected"] = live is not None
            pub["tools"] = [{"name": t["name"], "description": clip(t.get("description") or "", 200),
                             "read_only": bool((t.get("annotations") or {}).get("readOnlyHint"))} for t in (live.tools if live else [])]
            out.append(pub)
        return json_response({"connectors": out, "can_add_programs": req.account["role"] == "owner"})

    async def add_connector(self, req):
        body = req.json()
        if body.get("transport") == "stdio":
            self._confirm_host_access(req, body)
        entry = self.app.mcp.add(req.account, body)
        self.app.stats.event("connector_added", req.account["id"], window=req.window, transport=entry["transport"])
        return json_response({"connector": {"id": entry["id"], "name": entry["name"]}})

    async def update_connector(self, req):
        await self.app.mcp.set_enabled(req.account["id"], req.params["sid"], bool(req.json().get("enabled", True)))
        return json_response({"ok": True})

    async def remove_connector(self, req):
        await self.app.mcp.remove(req.account["id"], req.params["sid"])
        return json_response({"ok": True})

    async def test_connector(self, req):
        from ..mcp import MCPError
        cfg = next((c for c in self.app.mcp.configs(req.account["id"]) if c["id"] == req.params["sid"]), None)
        if cfg is None:
            raise KeyError(req.params["sid"])
        await self.app.mcp.disconnect(req.account["id"], cfg["name"])
        try:
            s = await self.app.mcp.server(req.account["id"], cfg["name"])
        except MCPError as exc:
            return json_response({"ok": False, "error": str(exc)})
        return json_response({"ok": True, "server": s.info.get("serverInfo"), "tools": len(s.tools)})

    # models ---------------------------------------------------------------------------------------
    async def models(self, req):
        reg = self.app.registry
        chat = [_model_public(m) for m in reg.approved("chat")]
        out = {"models": chat, "default": reg.default_chat(), "embed": reg.embed_model(),
               "images": [_model_public(m) for m in reg.approved("image")], "image_default": reg.image_model(),
               "stt": reg.audio_model()}
        if req.account["role"] == "owner":
            out["installed"] = [_model_public(m, full=True) for m in reg.all()]
            out["jobs"] = [j for j in self.app.maindb.jobs_recent(30)]
            mem = self.app.gpu.memory()
            out["gpu"] = {"name": self.app.gpu.name, "vram_total": mem["total"] if mem else None,
                          "vram_used": mem["used"] if mem else None, "others": self.app.gpu_others(),
                          "paused": self.app.gateway.queue.paused, "llamacpp": self.app.llamacpp.running(),
                          "ollama_ram_caps": await asyncio.to_thread(self.app.ollama_ram_caps)}
            out["settings"] = self._machine_settings()
            out["scout"] = self._scout_state()
        return json_response(out)

    async def models_sync(self, req):
        self._owner(req)
        await self.app.registry.sync()
        return await self.models(req)

    async def models_test(self, req):
        self._owner(req)
        name = req.json().get("model") or ""
        if self.app.registry.get(name) is None:
            raise KeyError(name)
        return json_response({"job": self.app.jobs.start_fit(name, req.account["id"])})

    async def models_approve(self, req):
        self._owner(req)
        body = req.json()
        m = self.app.registry.approve(body.get("model") or "", bool(body.get("approved", True)),
                                      int(body["num_ctx"]) if body.get("num_ctx") else None)
        self.app.stats.event("model_approved" if m["approved"] else "model_unapproved", req.account["id"], window=req.window,
                             model=m["name"], num_ctx=m["num_ctx"])
        return json_response({"model": _model_public(m, full=True)})

    async def models_pull(self, req):
        self._owner(req)
        name = (req.json().get("model") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._/:-]{1,200}", name):
            raise ValueError("Not a model name")
        return json_response({"job": self.app.jobs.start_pull(name, req.account["id"])})

    async def models_remove(self, req):
        self._owner(req)
        name = req.json().get("model") or ""
        m = self.app.registry.get(name)
        if m is None:
            raise KeyError(name)
        if m["runtime"] != "ollama":
            await self.app.gateway.unload(name)
            self.app.registry.remove_external(name)  # the program and model files are left where they are
        else:
            await self.app.jobs.remove(name)
        self.app.stats.event("model_removed", req.account["id"], window=req.window, model=name)
        return json_response({"ok": True})

    async def models_scout(self, req):
        """Find models: newer versions of the installed ones (all, or `model`), or a search by `query` or
        `category`."""
        self._owner(req)
        body = req.json()
        model = body.get("model") or None
        if model is not None and self.app.registry.get(model) is None:
            raise KeyError(model)
        query = " ".join(str(body.get("query") or "").split())[:200] or None
        category = body.get("category") or None
        if category is not None and category not in SCOUT_CATEGORIES:
            raise ValueError("Unknown category")
        return json_response({"job": self.app.jobs.start_scout(req.account["id"], bool(body.get("other_families")),
                                                               model=model, query=query, category=category)})

    async def models_scout_dismiss(self, req):
        """Hide one suggestion for good (`key`, as scout.rec_key makes it), or clear a finished search's
        results from the page (`clear`: its job id)."""
        self._owner(req)
        body = req.json()
        db = self.app.maindb
        if body.get("key"):
            key = str(body["key"])[:300]
            keys = [k for k in (db.get_setting("scout_dismissed") or []) if k != key]
            db.set_setting("scout_dismissed", (keys + [key])[-500:])
        if body.get("clear"):
            db.set_setting("scout_cleared", str(body["clear"])[:64])
        return json_response(self._scout_state())

    def _scout_state(self) -> dict:
        db = self.app.maindb
        return {"dismissed": db.get_setting("scout_dismissed") or [], "cleared": db.get_setting("scout_cleared"),
                "categories": [{"id": k, "label": v[0]} for k, v in SCOUT_CATEGORIES.items()]}

    async def models_external(self, req):
        """Register a model served by a llama.cpp server program (GGUF), then test its GPU fit."""
        body = req.json()
        self._confirm_host_access(req, body)
        runtime = body.get("runtime") or "llamacpp"
        common = {"server": str(body.get("server") or "").strip(),
                  "lib_dirs": [str(d).strip() for d in (body.get("lib_dirs") or []) if str(d).strip()],
                  "args": [str(a) for a in (body.get("args") or [])]}
        if runtime == "sdcpp":
            source = {**common, **{k: str(body.get(k) or "").strip() or None for k in ("diffusion_model", "llm", "vae", "llm_vision")},
                      "staged": bool(body.get("staged")), "edit": bool(body.get("edit")),
                      "steps": int(body["steps"]) if body.get("steps") else None,
                      "cfg": float(body["cfg"]) if body.get("cfg") not in (None, "") else None}
            m = self.app.registry.add_sdcpp(str(body.get("name") or "").strip(), source)
        elif runtime == "llamacpp":
            source = {**common, "model": str(body.get("model") or "").strip(), "mmproj": (body.get("mmproj") or "").strip() or None}
            m = self.app.registry.add_llamacpp(str(body.get("name") or "").strip(), source)
        else:
            raise ValueError("runtime is llamacpp or sdcpp")
        self.app.stats.event("model_registered", req.account["id"], window=req.window, model=m["name"], runtime=runtime)
        job = self.app.jobs.start_fit(m["name"], req.account["id"]) if body.get("test", True) else None
        return json_response({"model": _model_public(m, full=True), "job": job})

    async def gpu_pause(self, req):
        self._owner(req)
        body = req.json()
        info = None
        if body.get("paused"):
            info = {"by": req.account["display_name"], "since_ms": now_ms(), "reason": clip(str(body.get("reason") or ""), 200)}
        self.app.maindb.set_setting("gpu_paused", info)
        self.app.gateway.queue.pause(info)
        if info:
            await self.app.llamacpp.stop()
            for entry in await self._safe_ps():
                await self.app.gateway.unload(entry.get("name"))
        self.app.stats.event("gpu_paused" if info else "gpu_resumed", req.account["id"], window=req.window)
        return json_response({"paused": info})

    async def _safe_ps(self) -> list[dict]:
        try:
            return await self.app.ollama.ps()
        except (OllamaError, OSError, asyncio.TimeoutError):
            return []

    async def jobs(self, req):
        self._owner(req)
        return json_response({"jobs": self.app.maindb.jobs_recent(50)})

    async def cancel_job(self, req):
        self._owner(req)
        return json_response({"cancelled": self.app.jobs.cancel(req.params["jid"])})

    def _machine_settings(self) -> dict:
        db = self.app.maindb
        return {"default_model": db.get_setting("default_model"), "judge_model": db.get_setting("judge_model"),
                "keep_alive": db.get_setting("keep_alive", "30m"), "max_default_ctx": db.get_setting("max_default_ctx", 65536),
                "stats_retention_days": db.get_setting("stats_retention_days")}

    async def machine_settings(self, req):
        self._owner(req)
        return json_response({"settings": self._machine_settings()})

    async def update_machine_settings(self, req):
        self._owner(req)
        body = req.json()
        db = self.app.maindb
        names = [m["name"] for m in self.app.registry.approved("chat")]
        if "default_model" in body:
            if body["default_model"] and body["default_model"] not in names:
                raise ModelNotAllowed("Not an approved model")
            db.set_setting("default_model", body["default_model"] or None)
        if "judge_model" in body:
            if body["judge_model"] and body["judge_model"] not in names:
                raise ModelNotAllowed("Not an approved model")
            db.set_setting("judge_model", body["judge_model"] or None)
        if "keep_alive" in body and re.fullmatch(r"\d+[smh]?|-1", str(body["keep_alive"])):
            db.set_setting("keep_alive", str(body["keep_alive"]))
            self.app.gateway.keep_alive = str(body["keep_alive"])
        if "max_default_ctx" in body:
            db.set_setting("max_default_ctx", max(2048, int(body["max_default_ctx"])))
        if "stats_retention_days" in body:
            v = body["stats_retention_days"]
            db.set_setting("stats_retention_days", int(v) if v else None)
        self.app.events.broadcast("models.updated", {})
        return json_response({"settings": self._machine_settings()})

    # updates and restarts ---------------------------------------------------------------------------
    def _can_restart(self, req) -> None:
        if not self.app.restarter.allowed(req.account):
            raise PermissionError("Only an owner can restart or update baabaa here")

    async def update_status(self, req):
        from .. import update
        st = self.app.updates.status()
        can = self.app.restarter.allowed(req.account)
        owner = req.account["role"] == "owner"
        target_notes = (st.get("latest") or {}).get("notes") or []
        if st["state"] == "installed" and not target_notes:
            target_notes = update.changelog(update.target_root(), st.get("target"))
        out = {**st, "can_act": can, "owner": owner, "restart_by": self.app.maindb.get_setting("restart_by", "all"),
               "pending": self.app.restarter.public(), "restart_error": self.app.restarter.error,
               "work": self.app.restarter.busy(req.account["id"]) if can else None,
               "target_notes": target_notes, "running_notes": update.changelog(),
               "auto_check": os.environ.get("BAABAA_DISABLE_AUTOUPDATER") != "1"}
        if not owner:
            out.pop("previous", None)
        return json_response(out)

    async def update_check(self, req):
        self._owner(req)
        await self.app.updates.check(req.account)
        return await self.update_status(req)

    async def update_install(self, req):
        self._can_restart(req)
        pending = await self.app.updates.install(req.account, bool(req.json().get("now")))
        return json_response({"pending": pending})

    async def update_previous(self, req):
        self._owner(req)
        pending = await self.app.updates.previous(req.account, bool(req.json().get("now")))
        return json_response({"pending": pending})

    async def update_settings(self, req):
        from .. import update
        self._owner(req)
        body = req.json()
        if body.get("restart_by") in ("all", "owners"):
            self.app.maindb.set_setting("restart_by", body["restart_by"])
        if ("channel" in body or "checks" in body) and update.kind() == "installed":
            try:
                update.set_preferences(channel=body.get("channel"), checks=body.get("checks"))
            except update.UpdateError as exc:
                raise ValueError(str(exc)) from None
        self.app.updates._announce(force=True)
        return await self.update_status(req)

    async def restart(self, req):
        self._can_restart(req)
        st = self.app.updates.status()
        reason = "update" if st["state"] == "installed" else "changed" if st["state"] == "changed" else "restart"
        if self.network_state()["next_urls"]:
            reason = "network"
        return json_response({"pending": self.app.restarter.request(req.account, reason, bool(req.json().get("now")))})

    async def cancel_restart(self, req):
        self._can_restart(req)
        self.app.restarter.cancel(req.account)
        return json_response({"pending": None})

    async def local_restart(self, req):
        body = req.json()
        reason = body.get("reason") if body.get("reason") in ("update", "changed", "restart", "network") else "restart"
        return json_response({"pending": self.app.restarter.request(None, reason, bool(body.get("now")))})

    async def network(self, req):
        """Who can connect: this computer only, or the local network too (owners)."""
        self._owner(req)
        out = self.network_state()
        out["passwordless"] = [a["display_name"] for a in self.app.accounts.list() if not a["has_password"] and not a["disabled"]]
        out["remote"] = req.client_ip not in ("127.0.0.1", "::1", "local")
        return json_response(out)

    async def set_network(self, req):
        from .lan import MODES
        self._owner(req)
        mode = req.json().get("mode")
        if mode not in MODES:
            raise ValueError("The network mode is local or lan")
        self.app.maindb.set_setting("network", mode)
        self.app.stats.event("network_mode", req.account["id"], mode=mode)
        return await self.network(req)

    # statistics -----------------------------------------------------------------------------------
    def _stats_scope(self, req) -> str | None:
        """None = all accounts (owners only); otherwise one account id."""
        want = req.query.get("account")
        if req.account["role"] == "owner":
            return None if want in (None, "", "all") else want
        return req.account["id"]

    def _range(self, req) -> tuple[int, int]:
        now = now_ms()
        frm = int(req.query.get("from") or now - 30 * 86400 * 1000)
        to = int(req.query.get("to") or now + 60_000)
        return frm, to

    async def stats_summary(self, req):
        frm, to = self._range(req)
        data = self.app.stats.summary(self._stats_scope(req), frm, to, req.query.get("group", "day"),
                                      int(req.query.get("tz") or 0))
        if req.account["role"] == "owner" and data["group"] == "account_id":
            names = {a["id"]: a["display_name"] for a in self.app.accounts.list()}
            for g in data["groups"]:
                g["label"] = names.get(g["k"], g["k"])
        return json_response(data)

    async def stats_gpu(self, req):
        frm, to = self._range(req)
        return json_response({"samples": self.app.stats.gpu_series(frm, to)})

    async def stats_export(self, req):
        frm, to = self._range(req)
        table, fmt = req.query.get("table", "model_requests"), req.query.get("format", "csv")
        chunks = self.app.stats.export(table, fmt, self._stats_scope(req), frm, to)
        body = b"".join(chunks)
        self.app.stats.event("stats_export", req.account["id"], window=req.window, table=table, format=fmt)
        ext = "csv" if fmt == "csv" else "jsonl"
        ctype = "text/csv; charset=utf-8" if fmt == "csv" else "application/x-ndjson"
        return Response(body, content_type=ctype,
                        headers=[("Content-Disposition", f'attachment; filename="baabaa-{table}.{ext}"')])

    async def stats_snapshot(self, req):
        scope = self._stats_scope(req)
        with tempfile.TemporaryDirectory(dir=str(self.app.paths.root)) as tmp:
            dest = os.path.join(tmp, "stats.db")
            await asyncio.to_thread(self.app.stats.snapshot, dest, scope)
            with open(dest, "rb") as f:
                data = f.read()
        self.app.stats.event("stats_snapshot", req.account["id"], window=req.window)
        return Response(data, content_type="application/vnd.sqlite3",
                        headers=[("Content-Disposition", 'attachment; filename="baabaa-stats.sqlite"')])

    # accounts -------------------------------------------------------------------------------------
    async def list_accounts(self, req):
        self._owner(req)
        out = []
        for a in self.app.accounts.list():
            a["grants"] = self.app.accounts.grants(a["id"])
            out.append(a)
        return json_response({"accounts": out})

    async def create_account(self, req):
        self._owner(req)
        body = req.json()
        a = self.app.accounts.create(body.get("name") or "", body.get("display_name") or "", body.get("password") or None,
                                     body.get("role") or "user")
        self.app.stats.event("account_created", req.account["id"], window=req.window, role=a["role"])
        return json_response({"account": a})

    async def update_account(self, req):
        self._owner(req)
        body = req.json()
        aid = req.params["aid"]
        fields = {k: body[k] for k in ("display_name", "role", "require_owner_approval", "disabled", "color") if k in body}
        if "require_owner_approval" in fields:
            fields["require_owner_approval"] = int(bool(fields["require_owner_approval"]))
        if "disabled" in fields:
            fields["disabled"] = int(bool(fields["disabled"]))
        a = self.app.accounts.update(aid, **fields)
        if "password" in body:
            self.app.accounts.set_password(aid, body["password"] or None)
            a = self.app.accounts.get(aid)
        return json_response({"account": a})

    async def delete_account(self, req):
        self._owner(req)
        aid = req.params["aid"]
        if aid == req.account["id"]:
            raise ValueError("You cannot delete your own account")
        self.app.accounts.delete(aid)
        return json_response({"ok": True})

    async def add_grant(self, req):
        self._owner(req)
        body = req.json()
        self.app.accounts.grant(req.params["aid"], body.get("path") or "", body.get("access") or "rw")
        return json_response({"grants": self.app.accounts.grants(req.params["aid"])})

    async def remove_grant(self, req):
        self._owner(req)
        self.app.accounts.revoke(req.params["aid"], req.json().get("path") or "")
        return json_response({"grants": self.app.accounts.grants(req.params["aid"])})


def _model_public(m: dict, full: bool = False) -> dict:
    info = m["info"]
    out = {"name": m["name"], "role": m["role"], "approved": m["approved"], "num_ctx": m["num_ctx"],
           "runtime": m.get("runtime", "ollama"),
           "param_b": m["param_b"], "capabilities": info.get("capabilities") or [], "family": info.get("family"),
           "parameter_size": info.get("parameter_size"), "quantization": info.get("quantization"),
           "size": info.get("size"), "tokens_per_s": m["fit"].get("tokens_per_s"), "max_ctx": m["fit"].get("max_ctx")}
    if m["fit"].get("tools"):
        out["tool_use"] = {k: m["fit"]["tools"].get(k) for k in ("passed", "of", "checks")}
    if m["role"] == "image":
        out["seconds_1024"] = m["fit"].get("seconds_1024")
        out["seconds_512"] = m["fit"].get("seconds_512")  # fit tests before 2026-09-30 timed 512x512
        out["staged"] = bool(info.get("staged"))
    if full:
        out["fit"] = m["fit"]
        out["native_ctx"] = info.get("context_length")
        out["modified_at"] = info.get("modified_at")
        if m.get("runtime", "ollama") != "ollama":
            out["source"] = m.get("source")
    return out
