"""A stand-in for Ollama's native API in tests: scripted replies, fake residency, deterministic embeddings.

No model and no GPU. Start it with `FakeOllama().start()`; set replies with `.script([...])`, where each
reply is {"content": str} or {"tool_calls": [{"name": ..., "arguments": {...}}]} (optionally "thinking"), or
{"error": str} to end the reply with that error after any thinking and content, as Ollama does when it cannot
read a tool call. Requests are kept in `.requests` for assertions.
"""

import hashlib
import json
import math
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHAT_MODEL = "fake:4b"
EMBED_MODEL = "fake-embed:1m"


def fake_vector(text: str, dim: int = 64) -> list[float]:
    """Bag-of-words hashing: texts sharing words get similar vectors (enough to test retrieval)."""
    v = [0.0] * dim
    for word in text.lower().split():
        w = "".join(ch for ch in word if ch.isalnum())
        if len(w) < 3:
            continue
        h = int(hashlib.sha256(w[:6].encode()).hexdigest(), 16)
        v[h % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class FakeOllama:
    def __init__(self):
        self.replies: list[dict] = []
        self.requests: list[dict] = []
        self.loaded: dict[str, int] = {}
        self.lock = threading.Lock()
        self.server = None
        self.audio = False  # report the chat model as able to hear audio
        self.vision = False  # ... and to see images
        self.files: dict[str, list[str]] = {}  # model -> its files, reported as FROM lines by /api/show
        self.hang = 0  # this many chat requests get no answer until the model is unloaded (Ollama 0.30.7 did that)
        self.delay = 0.0  # seconds between streamed chunks, to watch a reply being written
        self.freed = threading.Event()
        self.version = "0.30.7"

    def script(self, replies: list[dict]) -> None:
        with self.lock:
            self.replies = list(replies)

    def start(self) -> str:
        owner = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/version":
                    return self._send({"version": owner.version})
                if self.path == "/api/tags":
                    return self._send({"models": [
                        {"name": CHAT_MODEL, "digest": "d1", "size": 3 * 2**30,
                         "details": {"family": "fake", "parameter_size": "4B", "quantization_level": "Q4_K_M"}},
                        {"name": EMBED_MODEL, "digest": "d2", "size": 2**28, "details": {"family": "fake"}}]})
                if self.path == "/api/ps":
                    with owner.lock:
                        return self._send({"models": [{"name": n, "model": n, "size": 2**30, "size_vram": 2**30,
                                                       "context_length": ctx} for n, ctx in owner.loaded.items()]})
                self._send({"error": "not found"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                with owner.lock:
                    owner.requests.append({"path": self.path, **body})
                if self.path == "/api/show":
                    embed = body.get("model") == EMBED_MODEL
                    caps = (["completion", "tools", "thinking"] + (["audio"] if owner.audio else [])
                            + (["vision"] if owner.vision else []))
                    return self._send({"capabilities": ["embedding"] if embed else caps,
                                       "modelfile": "".join(f"FROM {p}\n" for p in owner.files.get(body.get("model"), [])),
                                       "model_info": {"general.architecture": "fake", "fake.context_length": 32768}})
                if self.path == "/api/generate" and body.get("keep_alive") == 0:
                    with owner.lock:
                        owner.loaded.pop(body.get("model"), None)
                    owner.freed.set()
                    return self._send({"done": True})
                if self.path == "/api/embed":
                    with owner.lock:
                        owner.loaded[body["model"]] = (body.get("options") or {}).get("num_ctx", 2048)
                    return self._send({"embeddings": [fake_vector(t) for t in body.get("input") or []]})
                if self.path == "/api/chat":
                    with owner.lock:
                        owner.loaded[body["model"]] = (body.get("options") or {}).get("num_ctx", 4096)
                    if not body.get("messages"):
                        return self._stream([{"done": True, "done_reason": "load"}])
                    with owner.lock:
                        hang = owner.hang > 0
                        if hang:
                            owner.hang -= 1
                            owner.freed.clear()
                    if hang:
                        owner.freed.wait(10)
                        self.close_connection = True
                        return
                    first = body["messages"][0]
                    if first.get("role") == "user" and str(first.get("content", "")).startswith("Write a short title"):
                        reply = {"content": "Test title"}  # conversation titles do not use the script
                    else:
                        with owner.lock:
                            reply = owner.replies.pop(0) if owner.replies else {"content": "(no scripted reply)"}
                    chunks = []
                    for part in re.findall(r"\S+\s*", reply.get("thinking") or ""):  # word by word, as models think
                        chunks.append({"message": {"role": "assistant", "content": "", "thinking": part}, "done": False})
                    if reply.get("content"):
                        for part in reply["content"].split(" "):
                            chunks.append({"message": {"role": "assistant", "content": part + " "}, "done": False})
                    if reply.get("error"):
                        return self._stream(chunks + [{"error": reply["error"]}])
                    final = {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                             "prompt_eval_count": 100, "eval_count": 10, "prompt_eval_duration": 10_000_000,
                             "eval_duration": 100_000_000, "total_duration": 120_000_000}
                    if reply.get("tool_calls"):
                        final["message"]["tool_calls"] = [{"function": {"name": c["name"], "arguments": c.get("arguments") or {}}}
                                                          for c in reply["tool_calls"]]
                    chunks.append(final)
                    return self._stream(chunks)
                self._send({"error": "not found"}, 404)

            def _stream(self, chunks):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Connection", "close")
                self.end_headers()
                for c in chunks:
                    self.wfile.write(json.dumps(c).encode() + b"\n")
                    self.wfile.flush()
                    if owner.delay and not c.get("done"):
                        time.sleep(owner.delay)
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
