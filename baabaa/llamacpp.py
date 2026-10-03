"""Models run by a llama.cpp server (`llama-server`) that baabaa starts and stops: models Ollama cannot
run, and Ollama models whose turn carries an image or audio (see `ollama_engine`).

The owner registers such a model with two paths: the server program and the model file (GGUF). baabaa
runs one server at a time, on 127.0.0.1 and a free port, with a clean environment and the GPU rules
as settings:
- every layer on the first GPU (`-ngl 999 -dev CUDA0`: without a usable GPU the server fails instead of
  loading on the CPU), and `LLAMA_ARG_FIT=off` so that it fails rather than spill;
- the image/audio encoder on the GPU too (llama.cpp's default; `--no-mmproj-offload` is refused);
- weights read into GPU memory, not memory-mapped (`--no-mmap`);
- the KV cache on the GPU (the default; `-nkvo` is refused), so attention never runs on the CPU;
- bounded memory for llama.cpp's two RAM caches, which spare the GPU from reprocessing a conversation:
  the prompt cache (`--cache-ram 1024` MiB; llama.cpp's default is 8 GiB) and context checkpoints
  (`--ctx-checkpoints 4`, at least 256 tokens apart; default 32);
- two slots over one shared context (`-np 2 --kv-unified --no-cache-idle-slots`), so that a short side
  request (a conversation's title, the auto-mode judge) runs beside the conversation, whose context stays
  in VRAM instead of being swapped out and reprocessed;
- a fixed context (`-c`), one request at a time (baabaa's GPU queue);
- the model's own chat template (`--jinja`, needed for tool calls), and a random API key per start so
  that no other program on this computer can use the server;
- a load log detailed enough to show where every part went (`-lv 4`).
After loading, the log must show every layer, the KV cache and the encoder on the GPU and the caches
within their bounds, the process must hold the weights in VRAM (NVML, per process), and its system
memory must stay below a runaway ceiling (3 GiB beyond the input lookup tables: normal-program scale).
If a check fails, the server is stopped and the model refused. The input lookup tables (token
embeddings) stay in system memory, as in Ollama: llama.cpp looks each input token up there, one row per
token, which is not model computation. The CPU guard (cpuwatch.py) watches the rest.

Chat goes through the server's OpenAI-compatible endpoint and comes back as Ollama-style chunks, so
the rest of baabaa does not care which program runs a model.
"""

import asyncio
import base64
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import time

from .ollama import OllamaError

CACHE_RAM_MIB = 1024              # llama.cpp's RAM prompt cache, capped (its default is 8192)
CHECKPOINTS = 4                   # context checkpoints per slot, capped (its default is 32); the latest is the one used
CHECKPOINT_STEP = 256             # tokens between checkpoints, so that short conversations get them too
RAM_CEILING = 3 * 2**30           # system memory beyond the lookup tables that means something ran away
VRAM_SHARE = 0.97                 # the process must hold at least this share of the non-lookup weights
START_TIMEOUT = 600
GPU_DEVICE = re.compile(r"(CUDA|ROCm)\d+")

# Flags an owner may not add: they would put weights, computation or the KV cache off the GPU, or undo a
# setting baabaa makes itself.
FORBIDDEN_ARGS = {
    "-m", "--model", "--mmproj", "--host", "--port", "--api-key", "--api-key-file", "-c", "--ctx-size",
    "-np", "--parallel", "-ngl", "--gpu-layers", "--n-gpu-layers", "-dev", "--device", "-sm", "--split-mode",
    "-ts", "--tensor-split", "-mg", "--main-gpu", "-ot", "--override-tensor", "-nkvo", "--no-kv-offload",
    "-kvo", "--kv-offload", "-cram", "--cache-ram", "-ctxcp", "--ctx-checkpoints", "--swa-checkpoints",
    "-cms", "--checkpoint-min-step", "--cache-idle-slots", "--no-cache-idle-slots", "-kvu", "--kv-unified",
    "-no-kvu", "--no-kv-unified", "--slot-save-path", "--mlock", "--mmap", "--no-mmap",
    "-lm", "--load-mode", "-dio", "--direct-io", "-fit", "--fit", "-lv", "--log-verbosity", "--verbosity",
    "--log-file", "-v", "--verbose"}


def forbidden_args(args) -> list[str]:
    bad = []
    for a in args or []:
        a = str(a)
        if not a.startswith("-"):
            continue
        name = a.split("=", 1)[0]
        if name in FORBIDDEN_ARGS or any(w in name for w in ("cpu", "offload", "draft")):
            bad.append(name)
    return bad


class LlamaCppError(OllamaError):
    pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def memory(pid: int) -> dict | None:
    """A process's resident system memory in bytes: `anon` (its own copies), `shmem` (shared and pinned
    buffers, which is where CUDA keeps pinned host memory) and `file` (mapped files: program code)."""
    out = {}
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                key = {"RssAnon:": "anon", "RssShmem:": "shmem", "RssFile:": "file"}.get(line.split(" ", 1)[0].split("\t")[0])
                if key:
                    out[key] = int(line.split()[1]) * 1024
    except OSError:
        return None
    return out or None


def rss_anon(pid: int) -> int | None:
    """Memory a process holds in system RAM besides mapped files: anonymous plus shared/pinned, in bytes."""
    m = memory(pid)
    return None if m is None else m.get("anon", 0) + m.get("shmem", 0)


_HELP: dict = {}


def help_text(server: str, env: dict) -> str:
    """The server's --help, once per program file: some settings exist only in newer builds."""
    try:
        st = os.stat(server)
    except OSError:
        return ""
    key = (server, st.st_mtime_ns, st.st_size)
    if key not in _HELP:
        try:
            out = subprocess.run([server, "--help"], env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
            _HELP[key] = (out.stdout + out.stderr).decode(errors="replace")
        except (OSError, subprocess.SubprocessError):
            _HELP[key] = ""
    return _HELP[key]


def command(source: dict, num_ctx: int, port: int, api_key: str = "", help: str | None = None) -> tuple[list[str], dict]:
    """The server's argv and environment for `source` = {server, model, lib_dirs?, backend?, args?, mmproj?}.

    `help`: the server's --help text; settings a build does not know are left out (a build without
    `--cache-ram` has no RAM prompt cache, one without `--ctx-checkpoints` no checkpoints).
    """
    server, model = source["server"], source["model"]
    bad = forbidden_args(source.get("args"))
    if bad:
        raise LlamaCppError(f"{', '.join(bad)}: baabaa sets GPU placement, context and caching itself, by the GPU rules")
    has = (lambda flag: True) if not help else (lambda flag: re.search(rf"(^|[\s,]){re.escape(flag)}\b", help) is not None)
    # Two slots over one shared context (`--kv-unified`): a short side request (a title, the auto-mode judge)
    # runs in the second slot and the conversation's context stays in VRAM, instead of being swapped out.
    slots = 2 if has("--kv-unified") else 1
    argv = [server, "-m", model, "--host", "127.0.0.1", "--port", str(port), "-c", str(int(num_ctx)),
            "-np", str(slots), "-ngl", "999", "--no-mmap", "--jinja"]
    if slots == 2:
        argv.append("--kv-unified")
        if has("--no-cache-idle-slots"):
            argv.append("--no-cache-idle-slots")  # keep an idle conversation in VRAM, not moved to RAM
    if has("--device"):
        argv += ["-dev", source.get("device") or "CUDA0"]
    if has("--cache-ram"):
        argv += ["--cache-ram", str(CACHE_RAM_MIB)]
    if has("--ctx-checkpoints"):
        argv += ["--ctx-checkpoints", str(CHECKPOINTS)]
    if has("--checkpoint-min-step"):
        argv += ["--checkpoint-min-step", str(CHECKPOINT_STEP)]  # some builds default to 8192: none in a short chat
    if has("--threads"):
        argv += ["-t", "1"]  # every layer is on the GPU: more threads only spin waiting (see gateway.OLLAMA_THREADS)
    if has("--log-verbosity"):
        argv += ["-lv", "4"]
    if has("--no-webui"):
        argv.append("--no-webui")
    if source.get("mmproj"):
        argv += ["--mmproj", source["mmproj"]]
    argv += [str(a) for a in (source.get("args") or [])]
    lib = [os.path.dirname(server)] + [d for d in (source.get("lib_dirs") or []) if d]
    env = {"HOME": source.get("home") or "/tmp", "PATH": "/usr/bin:/bin", "LD_LIBRARY_PATH": ":".join(lib),
           "LLAMA_ARG_FIT": "off", "LC_ALL": "C.UTF-8",
           "CUDA_CACHE_MAXSIZE": str(2**30)}  # room for the GPU code compiled on first use, kept under HOME
    if source.get("backend"):
        env["GGML_BACKEND_PATH"] = source["backend"]  # Ollama's build loads its CUDA library from here
    if api_key:
        env["LLAMA_API_KEY"] = api_key  # in the environment, not the command line that `ps` shows
    return argv, env


def parse_log(text: str) -> dict:
    """Where the server's log says each part went (the details need `-lv 4`)."""
    out = {}
    m = re.findall(r"offloaded (\d+)/(\d+) layers to GPU", text)
    if m:
        out["offloaded"] = [int(m[-1][0]), int(m[-1][1])]
    for key, pattern in (("buffers_mib", r"\b([A-Za-z0-9_]+) +model buffer size =\s+([\d.]+) MiB"),
                         ("kv_mib", r"\b([A-Za-z0-9_]+) +KV buffer size =\s+([\d.]+) MiB"),
                         ("rs_mib", r"\b([A-Za-z0-9_]+) +RS buffer size =\s+([\d.]+) MiB")):
        found = {}
        for name, mib in re.findall(pattern, text):
            found[name] = round(found.get(name, 0) + float(mib), 2)
        if found:
            out[key] = found
    clip = re.findall(r"CLIP using (\S+) backend", text)
    if clip:
        out["encoder"] = sorted(set(clip))
    m = re.findall(r"prompt cache is enabled, size limit: (\S+)", text)
    if m:
        out["ram_cache_mib"] = int(m[-1]) if m[-1].lstrip("-").isdigit() else -1  # -1: no limit
    elif "prompt cache is disabled" in text:
        out["ram_cache_mib"] = 0
    m = re.findall(r"context checkpoints enabled, max = (\d+)", text)
    if m:
        out["checkpoints"] = int(m[-1])
    elif "context checkpoints disabled" in text:
        out["checkpoints"] = 0
    return out


def on_gpu(buffer_name: str) -> bool:
    return GPU_DEVICE.fullmatch(buffer_name) is not None


class LlamaServer:
    """One running llama-server: one model at one context size."""

    def __init__(self, name: str, source: dict, num_ctx: int, log_path: str, gpu, facts: dict):
        self.name, self.source, self.num_ctx = name, source, int(num_ctx)
        self.log_path, self.gpu, self.facts = log_path, gpu, facts
        self.port = _free_port()
        self.api_key = secrets.token_urlsafe(24)
        self.proc: asyncio.subprocess.Process | None = None
        self.load: dict = {}

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def _log_tail(self, n: int = 12) -> str:
        try:
            with open(self.log_path, errors="replace") as f:
                return "\n".join(f.read()[-6000:].strip().splitlines()[-n:])
        except OSError:
            return ""

    async def start(self) -> dict:
        for path in (self.source["server"], self.source["model"]):
            if not os.path.exists(path):
                raise LlamaCppError(f"{path} does not exist")
        _, env = command(self.source, self.num_ctx, self.port)
        helptext = await asyncio.to_thread(help_text, self.source["server"], env)
        argv, env = command(self.source, self.num_ctx, self.port, self.api_key, help=helptext)
        logf = open(self.log_path, "wb")
        try:
            self.proc = await asyncio.create_subprocess_exec(*argv, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                             stdout=logf, stderr=logf, start_new_session=True)
        finally:
            logf.close()
        t0 = time.monotonic()
        while time.monotonic() - t0 < START_TIMEOUT:
            if not self.alive:
                raise LlamaCppError(f"{self.name}: the server stopped while loading:\n{self._log_tail()}")
            try:
                status, _ = await self._get("/health", timeout=2)
                if status == 200:
                    break
            except (OSError, asyncio.TimeoutError, LlamaCppError):
                pass
            await asyncio.sleep(0.5)
        else:
            await self.stop()
            raise LlamaCppError(f"{self.name}: the server did not become ready in {START_TIMEOUT} s")
        self.load = {"load_ms": round((time.monotonic() - t0) * 1000), "pid": self.proc.pid, "port": self.port}
        await self.verify()
        return self.load

    async def verify(self) -> dict:
        """The GPU rules, checked on the running process. Stops the server and raises when one fails."""
        from .gateway import ResidencyError
        if not self.alive:
            raise ResidencyError(f"{self.name} is not running")
        try:
            with open(self.log_path, errors="replace") as f:
                log = parse_log(f.read())
        except OSError:
            log = {}
        weights = int(self.facts.get("weight_bytes") or 0)
        embd = int(self.facts.get("token_embd_bytes") or 0)  # the input lookup tables
        vram = self.gpu.process_used(self.proc.pid) if self.gpu else None
        mem = memory(self.proc.pid) or {}
        held = mem.get("anon", 0) + mem.get("shmem", 0) if mem else None
        self.load.update({"vram_bytes": vram, "rss_anon_bytes": held, "ram": mem, **log})
        problems = []
        if "offloaded" in log and log["offloaded"][0] < log["offloaded"][1]:
            problems.append(f"only {log['offloaded'][0]} of {log['offloaded'][1]} layers are on the GPU")
        host_weights = sum(v for k, v in (log.get("buffers_mib") or {}).items() if not on_gpu(k)) * 2**20
        if log.get("buffers_mib") and not any(on_gpu(k) for k in log["buffers_mib"]):
            problems.append("the model loaded without a GPU")
        elif host_weights > embd * 1.02 + 16 * 2**20:
            problems.append(f"{host_weights / 2**30:.2f} GiB of the weights are in system memory "
                            f"(only the {embd / 2**30:.2f} GiB of input lookup tables may be)")
        for key, what in (("kv_mib", "the KV cache"), ("rs_mib", "the recurrent state")):
            off = [k for k in (log.get(key) or {}) if not on_gpu(k)]
            if off:
                problems.append(f"{what} is in system memory ({', '.join(off)}), so its computation would run on the CPU")
        if any(not on_gpu(b) for b in log.get("encoder") or []):
            problems.append("the image/audio encoder runs on the CPU")
        cache, points = log.get("ram_cache_mib"), log.get("checkpoints")
        if (cache is not None and (cache < 0 or cache > CACHE_RAM_MIB)) or (points is not None and points > CHECKPOINTS):
            problems.append(f"its RAM caches are not bounded (prompt cache {'unlimited' if cache is not None and cache < 0 else f'{cache} MiB'}, "
                            f"{points} checkpoints; baabaa allows {CACHE_RAM_MIB} MiB and {CHECKPOINTS})")
        if vram is None:
            if "offloaded" not in log:
                problems.append("could not confirm that the model is on the GPU (no per-process GPU reading)")
        elif weights and vram < (weights - embd) * VRAM_SHARE:
            problems.append(f"the GPU holds {vram / 2**30:.2f} GiB, less than the model's {weights / 2**30:.2f} GiB of weights")
        if held is not None and weights and held > max(embd, host_weights) + RAM_CEILING:
            problems.append(f"the server holds {held / 2**30:.2f} GiB of system memory, beyond a normal program's")
        if problems:
            await self.stop()
            raise ResidencyError(f"{self.name} breaks the GPU rules: " + "; ".join(problems))
        return self.load

    async def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 20)
            except asyncio.TimeoutError:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self.proc.wait()

    # HTTP ----------------------------------------------------------------------------------------
    async def _open(self, method: str, path: str, body: dict | None, timeout: float = 30):
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.port, limit=2**22), timeout)
        payload = json.dumps(body).encode() if body is not None else b""
        writer.write((f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n"
                      f"Authorization: Bearer {self.api_key}\r\n"
                      f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload)
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        parts = line.decode("latin-1").split(" ", 2)
        if len(parts) < 2:
            writer.close()
            raise LlamaCppError("the llama.cpp server closed the connection")
        headers = {}
        while True:
            h = await asyncio.wait_for(reader.readline(), timeout)
            if h in (b"\r\n", b"\n", b""):
                break
            k, _, v = h.decode("latin-1").partition(":")
            headers[k.strip().lower()] = v.strip()
        return reader, writer, int(parts[1]), headers

    async def _get(self, path: str, timeout: float = 10) -> tuple[int, dict]:
        reader, writer, status, headers = await self._open("GET", path, None, timeout)
        try:
            raw = await asyncio.wait_for(reader.read(), timeout)
        finally:
            writer.close()
        if headers.get("transfer-encoding", "").lower() == "chunked":
            raw = _dechunk(raw)
        try:
            return status, json.loads(raw or b"{}")
        except ValueError:
            return status, {}

    async def chat(self, request: dict):
        """Ollama-style chat request in, Ollama-style chunks out (see to_openai and from_openai)."""
        body = to_openai(request)
        reader, writer, status, headers = await self._open("POST", "/v1/chat/completions", body, timeout=60)
        try:
            chunked = headers.get("transfer-encoding", "").lower() == "chunked"
            if status >= 400:
                raw = await reader.read()
                raw = _dechunk(raw) if chunked else raw
                try:
                    err = json.loads(raw).get("error")
                    msg = err.get("message") if isinstance(err, dict) else err
                except (ValueError, AttributeError):
                    msg = raw.decode(errors="replace")[:300]
                raise LlamaCppError(f"llama.cpp server: {msg}", status)
            state = _StreamState()
            buf = b""
            async for data in _body(reader, chunked):
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == b"[DONE]":
                        continue
                    try:
                        item = json.loads(payload)
                    except ValueError:
                        continue
                    if "error" in item:
                        err = item["error"]
                        raise LlamaCppError(err.get("message") if isinstance(err, dict) else str(err), 500)
                    for chunk in state.feed(item):
                        yield chunk
            final = state.finish()
            if final:
                yield final
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def _body(reader, chunked: bool):
    if not chunked:
        while True:
            data = await reader.read(65536)
            if not data:
                return
            yield data
    while True:
        size_line = await reader.readline()
        if not size_line:
            return
        size = int(size_line.split(b";")[0].strip() or b"0", 16)
        if size == 0:
            return
        data = await reader.readexactly(size)
        await reader.readexactly(2)
        yield data


def _dechunk(raw: bytes) -> bytes:
    out, i = [], 0
    while i < len(raw):
        j = raw.find(b"\r\n", i)
        if j < 0:
            break
        size = int(raw[i:j].split(b";")[0] or b"0", 16)
        if size == 0:
            break
        out.append(raw[j + 2:j + 2 + size])
        i = j + 2 + size + 2
    return b"".join(out)


# format conversion -------------------------------------------------------------------------------
def media_part(b64: str) -> dict:
    """An OpenAI content part for base64 media, by its first bytes: WAV/MP3 audio or an image."""
    try:
        head = base64.b64decode(b64[:24])
    except ValueError:
        head = b""
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}}
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}}
    mime = ("image/jpeg" if head[:3] == b"\xff\xd8\xff" else "image/gif" if head[:4] == b"GIF8"
            else "image/webp" if head[:4] == b"RIFF" and head[8:12] == b"WEBP" else "image/png")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def to_openai(req: dict) -> dict:
    """An Ollama /api/chat request as an OpenAI chat-completions request for llama-server."""
    out_msgs, pending_ids = [], []
    for m in req.get("messages") or []:
        role = m.get("role")
        if role == "tool":
            call_id = pending_ids.pop(0) if pending_ids else f"call_{len(out_msgs)}"
            out_msgs.append({"role": "tool", "tool_call_id": call_id, "content": m.get("content") or ""})
            continue
        msg = {"role": role, "content": m.get("content") or ""}
        if m.get("images"):  # Ollama's field for media: images, and audio for models that hear
            parts = [{"type": "text", "text": msg["content"]}] if msg["content"] else []
            parts += [media_part(b64) for b64 in m["images"]]
            msg["content"] = parts
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for i, c in enumerate(m["tool_calls"]):
                fn = c.get("function") or {}
                cid = f"call_{len(out_msgs)}_{i}"
                args = fn.get("arguments")
                calls.append({"id": cid, "type": "function", "function": {
                    "name": fn.get("name", ""), "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
            msg["tool_calls"] = calls
            pending_ids = [c["id"] for c in calls]
            if not msg["content"]:
                msg["content"] = None
        out_msgs.append(msg)
    body = {"messages": out_msgs, "stream": True, "stream_options": {"include_usage": True}}
    if req.get("tools"):
        body["tools"] = req["tools"]
    opts = req.get("options") or {}
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"), ("top_k", "top_k"), ("seed", "seed"),
                     ("num_predict", "max_tokens"), ("stop", "stop"), ("repeat_penalty", "repeat_penalty"),
                     ("min_p", "min_p")):
        if src in opts:
            body[dst] = opts[src]
    think = req.get("think")
    if think is not None:
        body["chat_template_kwargs"] = {"enable_thinking": bool(think)}
    fmt = req.get("format")
    if isinstance(fmt, dict):
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "reply", "schema": fmt}}
    elif fmt == "json":
        body["response_format"] = {"type": "json_object"}
    return body


class _StreamState:
    """Collects OpenAI stream deltas and yields Ollama-style chunks."""

    def __init__(self):
        self.calls: dict[int, dict] = {}
        self.finish_reason = None
        self.timings = {}
        self.usage = {}
        self.t0 = time.monotonic()

    def feed(self, item: dict):
        if item.get("timings"):
            self.timings = item["timings"]
        if item.get("usage"):
            self.usage = item["usage"]
        for choice in item.get("choices") or []:
            delta = choice.get("delta") or {}
            content, thinking = delta.get("content") or "", delta.get("reasoning_content") or ""
            for tc in delta.get("tool_calls") or []:
                slot = self.calls.setdefault(int(tc.get("index", len(self.calls))), {"name": "", "arguments": ""})
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            if content or thinking:
                msg = {"role": "assistant", "content": content}
                if thinking:
                    msg["thinking"] = thinking
                yield {"message": msg, "done": False}

    def finish(self) -> dict:
        calls = []
        for _, c in sorted(self.calls.items()):
            try:
                args = json.loads(c["arguments"]) if c["arguments"].strip() else {}
            except ValueError:
                args = {"_raw": c["arguments"]}
            calls.append({"function": {"name": c["name"], "arguments": args}})
        t = self.timings
        prompt_n = t.get("prompt_n")
        if prompt_n is None:
            prompt_n = self.usage.get("prompt_tokens")
        elif t.get("cache_n"):
            prompt_n += t["cache_n"]  # Ollama counts the whole prompt, cached part included
        done_reason = {"stop": "stop", "tool_calls": "stop", "length": "length"}.get(self.finish_reason or "stop", "stop")
        final = {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": done_reason,
                 "prompt_eval_count": prompt_n,
                 "eval_count": t.get("predicted_n", self.usage.get("completion_tokens")),
                 "prompt_eval_duration": int((t.get("prompt_ms") or 0) * 1e6) or None,
                 "eval_duration": int((t.get("predicted_ms") or 0) * 1e6) or None,
                 "total_duration": int((time.monotonic() - self.t0) * 1e9)}
        if calls:
            final["message"]["tool_calls"] = calls
        return final


class LlamaCppManager:
    """At most one llama-server runs at a time; it stops after `keep_alive` seconds without use."""

    def __init__(self, paths, gpu):
        self.paths, self.gpu = paths, gpu
        self.current: LlamaServer | None = None
        self._idle: asyncio.TimerHandle | None = None
        self._lock = asyncio.Lock()

    def running(self) -> dict | None:
        s = self.current
        if s is None or not s.alive:
            return None
        return {"name": s.name, "num_ctx": s.num_ctx, "pid": s.proc.pid, **s.load}

    async def ensure(self, name: str, source: dict, num_ctx: int, facts: dict) -> LlamaServer:
        key = (name, int(num_ctx), json.dumps(source, sort_keys=True))
        async with self._lock:
            s = self.current
            if s and s.alive and s.key == key:
                return s
            await self._stop_current()
            log_dir = self.paths.root / "runtime"
            log_dir.mkdir(exist_ok=True, mode=0o700)
            s = LlamaServer(name, {**source, "home": str(log_dir)}, num_ctx, str(log_dir / "llama-server.log"),
                            self.gpu, facts)
            s.key = key
            self.current = s
            try:
                await s.start()
            except BaseException:
                await s.stop()
                self.current = None
                raise
            return s

    def touch(self, keep_alive_s: float | None) -> None:
        if self._idle:
            self._idle.cancel()
            self._idle = None
        loop = asyncio.get_running_loop()
        if keep_alive_s == 0:  # as in Ollama: 0 unloads at once
            self._idle = loop.call_soon(lambda: loop.create_task(self.stop()))
        elif keep_alive_s and keep_alive_s > 0:
            self._idle = loop.call_later(keep_alive_s, lambda: loop.create_task(self.stop()))

    async def _stop_current(self) -> None:
        if self.current is not None:
            await self.current.stop()
            self.current = None

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_current()


# Ollama models with images or audio -----------------------------------------------------------------
# Ollama (0.30 and later) runs every model with its own copy of llama-server, but starts it with
# `--no-mmproj-offload`: the image/audio encoder then sits in system memory and runs on the CPU, which
# the GPU rules forbid. For a turn with an image or audio, baabaa runs the same model files with the
# same program itself, with every part on the GPU and the checks above.
OLLAMA_DIRS = ("/usr/local/lib/ollama", "/usr/lib/ollama")


def ollama_engine(cuda_driver: int | None) -> dict | None:
    """Ollama's llama-server and the CUDA build the driver can run: {server, backend, lib_dirs} or None."""
    order = ("cuda_v13", "cuda_v12") if (cuda_driver or 0) >= 13000 else ("cuda_v12",)
    for base in OLLAMA_DIRS:
        server = os.path.join(base, "llama-server")
        if not os.access(server, os.X_OK):
            continue
        for sub in order:
            backend = os.path.join(base, sub, "libggml-cuda.so")
            if os.path.exists(backend):
                return {"server": server, "backend": backend, "lib_dirs": [base, os.path.join(base, sub)]}
    return None


def ollama_files(show: dict) -> dict | None:
    """An Ollama model's files from /api/show: the model and, for a model that sees or hears, the encoder
    (a second FROM line, or inside the model file itself). None when the files cannot be read."""
    paths = [line[5:].strip() for line in (show.get("modelfile") or "").splitlines() if line.startswith("FROM /")]
    if not paths or not all(os.access(p, os.R_OK) for p in paths):
        return None
    caps = show.get("capabilities") or []
    media = "vision" in caps or "audio" in caps
    return {"model": paths[0], "mmproj": paths[1] if len(paths) > 1 else (paths[0] if media else None)}


def keep_alive_seconds(value) -> float | None:
    """Ollama-style keep_alive ('30m', '5m', 300, 0, -1) in seconds; None = forever."""
    if value is None:
        return 1800.0
    if isinstance(value, (int, float)):
        return None if value < 0 else float(value)
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*([smh]?)\s*", str(value))
    if not m:
        return 1800.0
    n = float(m.group(1))
    if n < 0:
        return None
    return n * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
