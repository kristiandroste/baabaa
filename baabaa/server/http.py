"""A small HTTP/1.1 server on asyncio: keep-alive, JSON, static files, streaming and SSE.

No framework: requests are parsed here, routed by method and path pattern, and answered with a
Response (bytes) or a StreamResponse (an async iterator of bytes, e.g. Server-Sent Events).
"""

import asyncio
import email.utils
import hashlib
import json
import mimetypes
import os
import re
import time
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("image/svg+xml", ".svg")

MAX_HEADER = 64 * 1024
MAX_BODY = 256 * 1024 * 1024
HEADER_TIMEOUT = 30
IDLE_TIMEOUT = 75


class HTTPError(Exception):
    def __init__(self, status: int, message: str = "", data: dict | None = None):
        super().__init__(message or HTTPStatus(status).phrase)
        self.status = status
        self.message = message or HTTPStatus(status).phrase
        self.data = data or {}


class Headers:
    def __init__(self, items=None):
        self._items: list[tuple[str, str]] = list(items or [])

    def get(self, name: str, default=None):
        n = name.lower()
        for k, v in self._items:
            if k.lower() == n:
                return v
        return default

    def add(self, name: str, value: str) -> None:
        self._items.append((name, value))

    def set(self, name: str, value: str) -> None:
        n = name.lower()
        self._items = [(k, v) for k, v in self._items if k.lower() != n]
        self._items.append((name, value))

    def items(self):
        return list(self._items)


class Request:
    def __init__(self, method, target, version, headers: Headers, body: bytes, client_ip: str, transport: str):
        self.method = method
        self.target = target
        self.version = version
        self.headers = headers
        self.body = body
        self.client_ip = client_ip
        self.transport = transport  # "tcp" or "unix"
        parts = urlsplit(target)
        self.path = unquote(parts.path)
        self.query = dict(parse_qsl(parts.query, keep_blank_values=True))
        self.params: dict[str, str] = {}
        self.session = None
        self.account = None
        self._json = None

    def json(self):
        if self._json is None:
            if not self.body:
                self._json = {}
            else:
                try:
                    self._json = json.loads(self.body)
                except ValueError as exc:
                    raise HTTPError(400, "Invalid JSON") from exc
        return self._json

    def cookie(self, name: str):
        raw = self.headers.get("cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    @property
    def window(self) -> str:
        return (self.headers.get("x-baabaa-client") or ("terminal" if self.transport == "unix" else "browser"))[:20]


class Response:
    def __init__(self, body=b"", status: int = 200, content_type: str = "application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            content_type = "application/json; charset=utf-8"
        elif isinstance(body, str):
            body = body.encode()
        self.body = body
        self.status = status
        self.headers = Headers(headers)
        if content_type and not self.headers.get("content-type"):
            self.headers.set("Content-Type", content_type)


class StreamResponse:
    """A response whose body comes from an async iterator; the connection closes when it ends."""

    def __init__(self, chunks, status: int = 200, content_type: str = "text/event-stream", headers=None):
        self.chunks = chunks
        self.status = status
        self.headers = Headers(headers)
        self.headers.set("Content-Type", content_type)


def json_response(data, status=200, headers=None) -> Response:
    return Response(data, status=status, headers=headers)


def sse_event(event: str, data) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode()


class Router:
    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, object]] = []

    def add(self, method: str, pattern: str, handler) -> None:
        regex = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"
        self.routes.append((method, re.compile(regex), handler))

    def route(self, method: str, pattern: str):
        def deco(fn):
            self.add(method, pattern, fn)
            return fn
        return deco

    def match(self, method: str, path: str):
        allowed = False
        for m, regex, handler in self.routes:
            match = regex.match(path)
            if match:
                if m == method or (m == "GET" and method == "HEAD"):
                    return handler, match.groupdict()
                allowed = True
        if allowed:
            raise HTTPError(405)
        return None, None


def static_response(root: Path, rel: str, request: Request) -> Response:
    root = root.resolve()
    target = (root / rel.lstrip("/")).resolve()
    if not str(target).startswith(str(root) + os.sep) or not target.is_file():
        raise HTTPError(404)
    st = target.stat()
    etag = '"' + hashlib.sha1(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:16] + '"'
    headers = [("ETag", etag), ("Cache-Control", "no-cache")]
    if request.headers.get("if-none-match") == etag:
        return Response(b"", status=304, content_type="", headers=headers)
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json", "image/svg+xml"):
        ctype += "; charset=utf-8"
    if target.suffix == ".js":
        ctype = "text/javascript; charset=utf-8"
    return Response(target.read_bytes(), content_type=ctype, headers=headers)


class Server:
    """Accepts connections, parses requests and hands them to `app(request) -> Response`."""

    def __init__(self, app, log=None):
        self.app = app
        self.log = log or (lambda *a: None)
        self.servers = []
        self.connections: set[asyncio.Task] = set()

    async def listen_tcp(self, host: str, port: int, ssl_context=None):
        srv = await asyncio.start_server(self._client, host, port, ssl=ssl_context, limit=MAX_HEADER,
                                         reuse_address=True, backlog=128)
        self.servers.append(srv)
        return srv

    async def listen_unix(self, path: str):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        srv = await asyncio.start_unix_server(self._client, path, limit=MAX_HEADER)
        os.chmod(path, 0o660)
        self.servers.append(srv)
        return srv

    async def close(self):
        for s in self.servers:
            s.close()
        for t in list(self.connections):
            t.cancel()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        self.connections.add(task)
        peer = writer.get_extra_info("peername")
        transport = "unix" if not isinstance(peer, tuple) else "tcp"
        client_ip = peer[0] if isinstance(peer, tuple) else "local"
        try:
            first = True
            while True:
                try:
                    request = await asyncio.wait_for(self._read_request(reader, writer, client_ip, transport),
                                                     HEADER_TIMEOUT if first else IDLE_TIMEOUT)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
                    return
                except HTTPError as exc:
                    await self._write(writer, Response({"error": exc.message}, status=exc.status), keep_alive=False)
                    return
                if request is None:
                    return
                first = False
                keep_alive = request.version == "HTTP/1.1" and (request.headers.get("connection") or "").lower() != "close"
                started = time.monotonic()
                try:
                    response = await self.app(request)
                except HTTPError as exc:
                    response = Response({"error": exc.message, **exc.data}, status=exc.status)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # the server must survive handler bugs
                    self.log("error", f"{request.method} {request.path}: {exc!r}")
                    import traceback
                    traceback.print_exc()
                    response = Response({"error": "Internal error"}, status=500)
                if isinstance(response, StreamResponse):
                    await self._write_stream(writer, response)
                    return
                await self._write(writer, response, keep_alive, head=request.method == "HEAD")
                self.log("access", f"{client_ip} {request.method} {request.path} {response.status} "
                                   f"{(time.monotonic() - started) * 1000:.0f}ms")
                if not keep_alive:
                    return
        finally:
            self.connections.discard(task)
            try:
                writer.close()
            except (ConnectionError, OSError, RuntimeError):
                pass

    async def _read_request(self, reader, writer, client_ip, transport):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError as exc:
            raise HTTPError(431) from exc
        except asyncio.IncompleteReadError as exc:
            if not exc.partial:
                return None
            raise
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError as exc:
            raise HTTPError(400) from exc
        headers = Headers()
        for line in lines[1:]:
            if not line:
                continue
            k, sep, v = line.partition(":")
            if not sep:
                raise HTTPError(400)
            headers.add(k.strip(), v.strip())
        body = b""
        if headers.get("transfer-encoding", "").lower() == "chunked":
            parts, total = [], 0
            while True:
                size = int((await reader.readline()).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    await reader.readline()
                    break
                total += size
                if total > MAX_BODY:
                    raise HTTPError(413)
                parts.append(await reader.readexactly(size))
                await reader.readexactly(2)
            body = b"".join(parts)
        else:
            length = int(headers.get("content-length") or 0)
            if length > MAX_BODY:
                raise HTTPError(413)
            if length:
                if (headers.get("expect") or "").lower() == "100-continue":
                    writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    await writer.drain()
                body = await reader.readexactly(length)
        return Request(method.upper(), target, version, headers, body, client_ip, transport)

    async def _write(self, writer, response: Response, keep_alive: bool, head: bool = False):
        body = response.body if isinstance(response.body, bytes) else bytes(response.body)
        lines = [f"HTTP/1.1 {response.status} {_reason(response.status)}"]
        response.headers.set("Content-Length", str(len(body)))
        response.headers.set("Date", email.utils.formatdate(usegmt=True))
        response.headers.set("Connection", "keep-alive" if keep_alive else "close")
        for k, v in response.headers.items():
            lines.append(f"{k}: {v}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (b"" if head else body))
        await writer.drain()

    async def _write_stream(self, writer, response: StreamResponse):
        lines = [f"HTTP/1.1 {response.status} {_reason(response.status)}"]
        response.headers.set("Cache-Control", "no-cache")
        response.headers.set("Connection", "close")
        response.headers.set("X-Accel-Buffering", "no")
        for k, v in response.headers.items():
            lines.append(f"{k}: {v}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await writer.drain()
        chunks = response.chunks
        try:
            async for chunk in chunks:
                if chunk:
                    writer.write(chunk)
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            close = getattr(chunks, "aclose", None)
            if close:
                try:
                    await close()
                except Exception:
                    pass


def _reason(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Unknown"
