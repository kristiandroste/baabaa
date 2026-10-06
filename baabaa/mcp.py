"""MCP (Model Context Protocol) client: connect to tool servers over stdio or streamable HTTP.

Servers are configured per account (members may add HTTP servers; only owners may add local programs,
which run with the owner's own permissions). Their tools reach the model through `find_tools`: the model
searches, and matching tools become available in that conversation, so a small context window is not
filled with every schema up front.
"""

import asyncio
import itertools
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .util import clip, dumps, loads, new_id, now_ms, ssl_context

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "baabaa", "version": __version__}
TIMEOUT = 60


class MCPError(Exception):
    pass


class StdioTransport:
    def __init__(self, command: str, args: list[str], env: dict | None, cwd: str | None):
        self.command, self.args, self.env, self.cwd = command, args, env or {}, cwd
        self.proc = None
        self.pending: dict[int, asyncio.Future] = {}
        self.reader_task = None
        self.stderr_tail: list[str] = []

    async def start(self):
        env = {**{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TERM", "USER")}, **self.env}
        self.proc = await asyncio.create_subprocess_exec(
            self.command, *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=self.cwd, limit=2**24, start_new_session=True)
        self.reader_task = asyncio.get_running_loop().create_task(self._read())
        asyncio.get_running_loop().create_task(self._stderr())

    async def _read(self):
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            fut = self.pending.pop(msg.get("id"), None) if "id" in msg and ("result" in msg or "error" in msg) else None
            if fut and not fut.done():
                fut.set_result(msg)
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(MCPError("the server stopped" + (": " + " ".join(self.stderr_tail[-3:]) if self.stderr_tail else "")))

    async def _stderr(self):
        while self.proc and self.proc.stderr:
            line = await self.proc.stderr.readline()
            if not line:
                return
            self.stderr_tail = (self.stderr_tail + [line.decode(errors="replace").strip()])[-20:]

    async def send(self, msg: dict, expect_reply: bool):
        if self.proc is None or self.proc.returncode is not None:
            raise MCPError("the server is not running")
        fut = None
        if expect_reply:
            fut = asyncio.get_running_loop().create_future()
            self.pending[msg["id"]] = fut
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()
        if fut:
            return await asyncio.wait_for(fut, TIMEOUT)
        return None

    async def close(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 3)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
        if self.reader_task:
            self.reader_task.cancel()


class HttpTransport:
    """Streamable HTTP: each JSON-RPC message is a POST; the reply is JSON or an SSE stream."""

    def __init__(self, url: str, headers: dict | None, public_only: bool = False):
        self.url, self.headers = url, headers or {}
        self.public_only = public_only  # added by a member: never this computer or the local network
        self.session_id = None
        self.protocol = None

    async def start(self):
        pass

    def _post(self, msg: dict, expect_reply: bool):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=headers, method="POST")
        from .agent.web import FetchError, public_opener
        try:
            if self.public_only:
                opening = public_opener().open(req, timeout=TIMEOUT)
            else:
                opening = urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl_context())
            with opening as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                if not expect_reply:
                    return None
                ctype = resp.headers.get("Content-Type", "")
                if "text/event-stream" in ctype:
                    data = []
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif line == "" and data:
                            try:
                                m = json.loads("\n".join(data))
                            except ValueError:
                                m = {}
                            data = []
                            if m.get("id") == msg.get("id") and ("result" in m or "error" in m):
                                return m
                    raise MCPError("the server closed the stream without a reply")
                body = resp.read(16 * 2**20)
                return json.loads(body) if body.strip() else {}
        except FetchError as exc:
            raise MCPError(str(exc)) from None
        except urllib.error.HTTPError as exc:
            raise MCPError(f"HTTP {exc.code} from the server") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise MCPError(f"could not reach the server: {getattr(exc, 'reason', exc)}") from exc

    async def send(self, msg: dict, expect_reply: bool):
        return await asyncio.to_thread(self._post, msg, expect_reply)

    async def close(self):
        pass


class MCPServer:
    def __init__(self, config: dict):
        self.config = config
        self.transport = None
        self.tools: list[dict] = []
        self.info: dict = {}
        self.error: str | None = None
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.config["name"]

    async def connect(self):
        c = self.config
        if c["transport"] == "stdio":
            self.transport = StdioTransport(c["command"], c.get("args") or [], c.get("env"), c.get("cwd"))
        else:
            self.transport = HttpTransport(c["url"], c.get("headers"), public_only=bool(c.get("public_only")))
        await self.transport.start()
        res = await self.rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                            "clientInfo": CLIENT_INFO})
        self.info = res
        if isinstance(self.transport, HttpTransport):
            self.transport.protocol = res.get("protocolVersion", PROTOCOL_VERSION)
        await self.transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, False)
        await self.refresh_tools()

    async def rpc(self, method: str, params: dict | None = None):
        msg = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            msg["params"] = params
        reply = await self.transport.send(msg, True)
        if reply is None:
            raise MCPError("no reply")
        if "error" in reply:
            e = reply["error"]
            raise MCPError(f"{e.get('message', 'error')} ({e.get('code')})")
        return reply.get("result") or {}

    async def refresh_tools(self):
        tools, cursor = [], None
        for _ in range(20):
            res = await self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools

    async def call(self, tool: str, arguments: dict) -> dict:
        async with self._lock if isinstance(self.transport, HttpTransport) else _null():
            res = await self.rpc("tools/call", {"name": tool, "arguments": arguments or {}})
        parts = []
        for item in res.get("content", []):
            t = item.get("type")
            if t == "text":
                parts.append(item.get("text", ""))
            elif t == "resource":
                r = item.get("resource") or {}
                parts.append(r.get("text") or f"[resource {r.get('uri')}]")
            elif t in ("image", "audio"):
                parts.append(f"[{t} content, {item.get('mimeType')}]")
            elif t == "resource_link":
                parts.append(f"[link {item.get('uri')}: {item.get('name', '')}]")
        if res.get("structuredContent") and not parts:
            parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
        return {"text": "\n".join(parts) or "(no content)", "error": bool(res.get("isError"))}

    async def close(self):
        if self.transport:
            await self.transport.close()


class _null:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def tool_id(server: str, tool: str) -> str:
    return f"mcp__{_slug(server)}__{_slug(tool)}"[:64]


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_") or "x"


class MCPManager:
    """Servers per account, connected on first use and kept open."""

    def __init__(self, app):
        self.app = app
        self.live: dict[tuple[str, str], MCPServer] = {}

    def configs(self, account_id: str) -> list[dict]:
        return loads(self.app.stores.get(account_id).get_setting("mcp_servers"), []) or []

    def save(self, account_id: str, configs: list[dict]) -> None:
        self.app.stores.get(account_id).set_setting("mcp_servers", dumps(configs))

    def add(self, account: dict, cfg: dict) -> dict:
        name = (cfg.get("name") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}", name):
            raise ValueError("Give the server a short name (letters, digits, space, . _ -)")
        transport = cfg.get("transport")
        if transport == "stdio":
            if account["role"] != "owner":
                raise PermissionError("Only an owner can add programs that run on this computer")
            if not cfg.get("command"):
                raise ValueError("A command is needed")
            entry = {"transport": "stdio", "command": cfg["command"], "args": [str(a) for a in cfg.get("args") or []],
                     "env": {str(k): str(v) for k, v in (cfg.get("env") or {}).items()}, "cwd": cfg.get("cwd") or None}
        elif transport == "http":
            url = cfg.get("url") or ""
            if not re.match(r"^https?://", url):
                raise ValueError("The URL must start with http:// or https://")
            entry = {"transport": "http", "url": url, "headers": {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}}
            if account["role"] != "owner":  # a member's connector may not reach this computer or the local network
                from .agent.web import FetchError, _check_host
                try:
                    _check_host(urllib.parse.urlsplit(url).hostname or "")
                except FetchError:
                    raise PermissionError("Only an owner can connect baabaa to a server on this computer or the local network") from None
                entry["public_only"] = True
        else:
            raise ValueError("transport is stdio or http")
        configs = [c for c in self.configs(account["id"]) if c["name"] != name]
        entry.update({"id": new_id(), "name": name, "enabled": True, "added_ms": now_ms()})
        configs.append(entry)
        self.save(account["id"], configs)
        return entry

    async def remove(self, account_id: str, server_id: str) -> None:
        configs = self.configs(account_id)
        for c in configs:
            if c["id"] == server_id:
                await self.disconnect(account_id, c["name"])
        self.save(account_id, [c for c in configs if c["id"] != server_id])

    async def set_enabled(self, account_id: str, server_id: str, enabled: bool) -> None:
        configs = self.configs(account_id)
        for c in configs:
            if c["id"] == server_id:
                c["enabled"] = enabled
                if not enabled:
                    await self.disconnect(account_id, c["name"])
        self.save(account_id, configs)

    async def disconnect(self, account_id: str, name: str) -> None:
        s = self.live.pop((account_id, name), None)
        if s:
            await s.close()

    async def server(self, account_id: str, name: str) -> MCPServer:
        key = (account_id, name)
        s = self.live.get(key)
        if s and s.transport and not (isinstance(s.transport, StdioTransport) and s.transport.proc.returncode is not None):
            return s
        cfg = next((c for c in self.configs(account_id) if c["name"] == name and c.get("enabled", True)), None)
        if cfg is None:
            raise MCPError(f"no enabled server named {name}")
        s = MCPServer(cfg)
        try:
            await asyncio.wait_for(s.connect(), TIMEOUT)
        except (MCPError, OSError, asyncio.TimeoutError) as exc:
            await s.close()
            raise MCPError(f"{name}: {exc}") from exc
        self.live[key] = s
        return s

    async def all_tools(self, account_id: str) -> list[dict]:
        """[{'id', 'server', 'name', 'description', 'schema', 'read_only'}] for every enabled server that connects."""
        out = []
        for cfg in self.configs(account_id):
            if not cfg.get("enabled", True):
                continue
            try:
                s = await self.server(account_id, cfg["name"])
            except MCPError:
                continue
            for t in s.tools:
                ann = t.get("annotations") or {}
                out.append({"id": tool_id(cfg["name"], t["name"]), "server": cfg["name"], "name": t["name"],
                            "description": clip(t.get("description") or "", 600),
                            "schema": t.get("inputSchema") or {"type": "object", "properties": {}},
                            "read_only": bool(ann.get("readOnlyHint"))})
        return out

    async def search(self, account_id: str, query: str, limit: int = 6) -> list[dict]:
        words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
        scored = []
        for t in await self.all_tools(account_id):
            hay = f"{t['server']} {t['name']} {t['description']}".lower()
            score = sum(3 if w in t["name"].lower() else 1 for w in words if w in hay)
            if score or not words:
                scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[:limit]]

    async def call(self, account_id: str, tid: str, arguments: dict) -> dict:
        for t in await self.all_tools(account_id):
            if t["id"] == tid:
                s = await self.server(account_id, t["server"])
                return await s.call(t["name"], arguments)
        raise MCPError(f"no tool {tid}")

    async def close_all(self):
        for s in list(self.live.values()):
            await s.close()
        self.live.clear()
