"""Async client for Ollama's native API, written on asyncio streams (no HTTP library).

Streaming endpoints (/api/chat, /api/pull) return newline-delimited JSON over chunked transfer
encoding. Closing the connection cancels the work on Ollama's side, which is how Stop frees the GPU.
"""

import asyncio
import json
from urllib.parse import urlsplit


# Settings that Ollama (0.30 and later) passes on to the llama.cpp server it runs each model with: caps on
# the two caches that server keeps in system RAM. Without them it holds a prompt cache of up to 8 GiB and
# up to 32 context checkpoints per conversation, too much for a machine with 16 GB; with them, at most
# about 2 GB, and conversations still resume without being reprocessed.
RAM_CAPS = {"LLAMA_ARG_CACHE_RAM": 1024, "LLAMA_ARG_CTX_CHECKPOINTS": 8}


def service_environment(unit: str = "ollama") -> dict | None:
    """The environment of Ollama's systemd service, or None when there is no such service to read."""
    import shlex
    import subprocess
    import sys
    if sys.platform != "linux":
        return None
    try:
        out = subprocess.run(["systemctl", "show", unit, "--property=LoadState", "--property=Environment"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    props = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    if out.returncode != 0 or props.get("LoadState") != "loaded":
        return None
    env = {}
    try:
        items = shlex.split(props.get("Environment", ""))
    except ValueError:
        return None
    for item in items:
        k, sep, v = item.partition("=")
        if sep:
            env[k] = v
    return env


def ram_caps_missing(env: dict | None) -> list[str] | None:
    """The service settings still needed to cap those caches (a value at or below the cap counts as set);
    None when the service cannot be read."""
    if env is None:
        return None
    missing = []
    for key, cap in RAM_CAPS.items():
        try:
            value = int(env.get(key, ""))
        except ValueError:
            value = None
        if value is None or value < 0 or value > cap:  # absent, unlimited (-1) or above the cap
            missing.append(f"{key}={cap}")
    return missing


class OllamaError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# How long a generation may take to start answering: a long prompt, or a queue inside Ollama. The gateway
# notices a stuck Ollama much sooner (no answer while the GPU idles).
STREAM_WAIT = 900.0


class Ollama:
    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout: float = 30.0):
        u = urlsplit(base_url)
        if u.scheme != "http":
            raise ValueError("Ollama is reached over plain HTTP on localhost")
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or 11434
        self.timeout = timeout

    # plumbing -----------------------------------------------------------------------------------
    async def _open(self, method: str, path: str, body: dict | None, wait: float | None = None):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, limit=2**22), self.timeout)
        payload = json.dumps(body).encode() if body is not None else b""
        head = (
            f"{method} {path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
        )
        try:
            writer.write(head.encode() + payload)
            await writer.drain()
            try:
                status_line = await asyncio.wait_for(reader.readline(), wait or self.timeout)
            except asyncio.TimeoutError:
                raise OllamaError(f"Ollama did not start answering within {round(wait or self.timeout)} s") from None
            parts = status_line.decode("latin-1").split(" ", 2)
            if len(parts) < 2:
                raise OllamaError("Ollama closed the connection")
            status = int(parts[1])
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), self.timeout)
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
        except BaseException:  # failed, timed out or cancelled: never leave the connection open
            writer.close()
            raise
        return reader, writer, status, headers

    async def _body_chunks(self, reader, headers):
        if headers.get("transfer-encoding", "").lower() == "chunked":
            while True:
                size_line = await reader.readline()
                if not size_line:
                    return
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    await reader.readline()
                    return
                data = await reader.readexactly(size)
                await reader.readexactly(2)
                yield data
        elif "content-length" in headers:
            remaining = int(headers["content-length"])
            while remaining > 0:
                data = await reader.read(min(remaining, 65536))
                if not data:
                    return
                remaining -= len(data)
                yield data
        else:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                yield data

    async def _json(self, method: str, path: str, body: dict | None = None, timeout: float | None = None):
        async def run():
            reader, writer, status, headers = await self._open(method, path, body)
            try:
                raw = b"".join([c async for c in self._body_chunks(reader, headers)])
            finally:
                writer.close()
            data = json.loads(raw) if raw.strip() else {}
            if status >= 400:
                raise OllamaError(data.get("error") if isinstance(data, dict) else raw.decode()[:200], status)
            return data
        try:
            return await asyncio.wait_for(run(), timeout or self.timeout)
        except asyncio.TimeoutError:
            raise OllamaError(f"Ollama did not answer within {round(timeout or self.timeout)} s") from None

    async def _stream(self, path: str, body: dict, wait: float | None = None):
        """Yield one dict per NDJSON line. Raises OllamaError on HTTP errors or {"error": ...} lines."""
        reader, writer, status, headers = await self._open("POST", path, body, wait)
        try:
            if status >= 400:
                raw = b"".join([c async for c in self._body_chunks(reader, headers)])
                try:
                    msg = json.loads(raw).get("error", raw.decode()[:300])
                except ValueError:
                    msg = raw.decode(errors="replace")[:300]
                raise OllamaError(msg, status)
            buf = b""
            async for chunk in self._body_chunks(reader, headers):
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        item = json.loads(line)
                        if "error" in item:
                            raise OllamaError(item["error"], 500)
                        yield item
            if buf.strip():
                item = json.loads(buf)
                if "error" in item:
                    raise OllamaError(item["error"], 500)
                yield item
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    # API ----------------------------------------------------------------------------------------
    async def version(self) -> str:
        return (await self._json("GET", "/api/version")).get("version", "")

    async def tags(self) -> list[dict]:
        return (await self._json("GET", "/api/tags")).get("models", [])

    async def ps(self) -> list[dict]:
        return (await self._json("GET", "/api/ps")).get("models", [])

    async def show(self, model: str) -> dict:
        return await self._json("POST", "/api/show", {"model": model})

    async def delete(self, model: str) -> None:
        await self._json("DELETE", "/api/delete", {"model": model})

    async def unload(self, model: str) -> None:
        await self._json("POST", "/api/generate", {"model": model, "keep_alive": 0}, timeout=60)

    def chat(self, request: dict):
        return self._stream("/api/chat", {**request, "stream": True}, STREAM_WAIT)

    def generate(self, request: dict):
        return self._stream("/api/generate", {**request, "stream": True}, STREAM_WAIT)

    def pull(self, model: str):
        return self._stream("/api/pull", {"model": model, "stream": True})

    async def embed(self, model: str, inputs: list[str], options: dict | None = None, keep_alive=None) -> list:
        body = {"model": model, "input": inputs, "options": options or {}}
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        return (await self._json("POST", "/api/embed", body, timeout=300)).get("embeddings", [])
