"""The model gateway: one GPU queue, the residency guard, and per-request statistics.

Every model call in baabaa goes through `Gateway.chat`. It
- waits its turn in a single FIFO queue for the GPU (all accounts share it);
- sends `num_gpu: 999`, `num_thread: 1` (see OLLAMA_THREADS), the model's fixed `num_ctx`, `shift: false` and
  `truncate: false`;
- after the first chunk, checks /api/ps: the model must be 100% in VRAM (size_vram == size), otherwise
  the request is aborted and the model unloaded;
- records timing, tokens, energy and outcome in the statistics database;
- releases the GPU the moment the caller stops reading (cancellation closes the Ollama connection).
"""

import array
import asyncio
import itertools
import json
import os
import sys
import time
from dataclasses import dataclass, field

from . import cpuwatch, harmony, library
from .ollama import OllamaError
from .system import MAC
from .util import mono_ms


class ResidencyError(Exception):
    pass


class ModelNotAllowed(Exception):
    pass


class GpuSearchError(Exception):
    pass


# Ollama 0.30.7 can stop serving a model after a failed request (seen 2026-09-30 after a tool call it could not
# parse): later requests for it wait forever while the GPU idles. No answer for STUCK_IDLE_S of idle GPU means
# stuck, not busy; unloading the model frees it. Without a GPU reading, STUCK_BLIND_S of silence counts.
# Ollama fixed it (ollama#17825, merged 2026-08-19); on 0.35.0 the next request answered at once (measured
# 2026-10-01), so from FREEZE_FIXED on a failed reply no longer unloads the model.
STUCK_CHECK_S = 5
STUCK_IDLE_S = 45
STUCK_BLIND_S = 180
FREEZE_FIXED = (0, 35, 0)
VERSION_TTL_MS = 600_000
# CPU threads for Ollama's model server. With every layer on the GPU one thread does all the CPU's part; Ollama
# 0.35 otherwise starts one per core, and the extra five spin waiting for work: 3.5 cores busy against 1.0, at the
# same speed (qwen3.5:9b and nemotron-3-nano:4b, prompt and writing, measured 2026-10-01). A load-time setting:
# every request that may load a model sends the same value, so it never forces a reload.
OLLAMA_THREADS = 1
CUDA_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cuda.py")


@dataclass
class Ticket:
    kind: str
    model: str
    account_id: str | None = None
    conv_id: str | None = None
    label: str = ""
    background: bool = False  # work nobody is waiting for (indexing): it waits for a quiet GPU
    id: int = field(default_factory=itertools.count(1).__next__)
    started: float = field(default_factory=mono_ms)

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "model": self.model, "account_id": self.account_id,
                "conv_id": self.conv_id, "label": self.label}


class GpuQueue:
    """One holder at a time, first come first served. The owner can pause it (another program needs the
    GPU): work already running finishes or is stopped by its user; new work waits until resumed."""

    def __init__(self, on_change=None):
        self.holder: Ticket | None = None
        self.waiting: list[tuple[Ticket, asyncio.Future]] = []
        self.on_change = on_change
        self.paused: dict | None = None
        self.last_active = 0.0  # monotonic time interactive work last released the GPU

    def pause(self, info: dict | None) -> None:
        self.paused = info
        if info is None:
            self._grant_next()
        self._changed()

    def _grant_next(self) -> None:
        if self.holder is not None or self.paused:
            return
        while self.waiting:
            t, fut = self.waiting.pop(0)
            if not fut.done():
                self.holder = t
                fut.set_result(True)
                break

    def _changed(self):
        if self.on_change:
            try:
                self.on_change(self.snapshot())
            except Exception:  # listeners must not break the queue
                pass

    def snapshot(self) -> dict:
        return {"running": self.holder.public() if self.holder else None,
                "waiting": [t.public() for t, _ in self.waiting], "paused": self.paused}

    def position(self, conv_id: str) -> int | None:
        for i, (t, _) in enumerate(self.waiting):
            if t.conv_id == conv_id:
                return i + 1
        return None

    async def acquire(self, ticket: Ticket) -> None:
        if self.holder is None and not self.waiting and not self.paused:
            self.holder = ticket
            self._changed()
            return
        fut = asyncio.get_running_loop().create_future()
        entry = (ticket, fut)
        self.waiting.append(entry)
        self._changed()
        try:
            await fut
        except asyncio.CancelledError:
            if entry in self.waiting:
                self.waiting.remove(entry)
                self._changed()
            elif self.holder is ticket:
                self.release(ticket)
            raise

    def release(self, ticket: Ticket) -> None:
        if self.holder is not ticket:
            return
        self.holder = None
        if not ticket.background:
            self.last_active = time.monotonic()
        self._grant_next()
        self._changed()

    async def quiet(self, seconds: float) -> None:
        """Wait until nobody holds or waits for the GPU and no interactive work has run for `seconds`:
        background work then does not evict the model of a conversation between its turns."""
        while self.holder is not None or self.waiting or self.paused or time.monotonic() - self.last_active < seconds:
            await asyncio.sleep(1)


class GpuSampler:
    """Samples GPU readings every second while at least one model request is running."""

    def __init__(self, gpu, stats, interval: float = 1.0):
        self.gpu, self.stats, self.interval = gpu, stats, interval
        self.active = 0
        self._task = None

    def start(self):
        self.active += 1
        if getattr(self.gpu, "readings", self.gpu.available) and (self._task is None or self._task.done()):
            self._task = asyncio.get_running_loop().create_task(self._run())

    def stop(self):
        self.active = max(0, self.active - 1)

    async def _run(self):
        while self.active > 0:
            self.stats.gpu_sample(self.gpu.sample())
            await asyncio.sleep(self.interval)


class Gateway:
    def __init__(self, ollama, stats, gpu, registry=None, keep_alive: str = "30m", on_queue_change=None,
                 llamacpp=None, sdcpp=None):
        self.ollama = ollama
        self.stats = stats
        self.gpu = gpu
        self.registry = registry
        self.keep_alive = keep_alive
        self.queue = GpuQueue(on_queue_change)
        self.sampler = GpuSampler(gpu, stats)
        self.llamacpp = llamacpp   # LlamaCppManager: models served by llama.cpp instead of Ollama
        self.sdcpp = sdcpp         # SdManager: image models served by stable-diffusion.cpp
        self._media: dict = {}     # Ollama model -> how baabaa runs it for turns with images or audio
        self._files: dict = {}     # Ollama model -> its files (from /api/show), to find the process serving it
        self._version: tuple = ((), -VERSION_TTL_MS)  # Ollama's version and when it was read
        # the CPU guard (cpuwatch.py); tests lower these
        self.cpu_limit, self.cpu_window, self.cpu_interval = cpuwatch.LIMIT_CORES, cpuwatch.WINDOW_S, cpuwatch.INTERVAL_S

    async def _make_room(self, runtime: str) -> None:
        """Only one program holds the GPU: loading for one runtime stops or unloads the others."""
        if runtime != "sdcpp" and self.sdcpp is not None and self.sdcpp.running():
            await self.sdcpp.stop()
        if runtime != "llamacpp" and self.llamacpp is not None and self.llamacpp.running():
            await self.llamacpp.stop()
        if runtime != "ollama":
            await self._unload_ollama_all()

    def _model(self, model: str) -> dict | None:
        return self.registry.get(model) if self.registry else None

    def runtime(self, model: str) -> str:
        m = self._model(model)
        return m["runtime"] if m else "ollama"

    async def ensure_loaded(self, model: str, num_ctx: int, keep_alive=None, media: dict | None = None) -> dict:
        """Load `model` at `num_ctx` without generating anything, then require 100% VRAM residency.

        Ollama loads a model on a chat request with no messages and returns at once. Checking /api/ps
        before any prompt is processed means no inference ever runs on a partially offloaded model.
        A llama.cpp model gets its own server, checked the same way (llamacpp.py). The two programs
        never hold the GPU at the same time: loading one unloads the other. `media` (from
        `media_route`): run an Ollama model's files with baabaa's own server, encoder on the GPU.
        """
        if media is not None:
            await self._make_room("llamacpp")
            server = await self._ensure_media(model, media)
            self.llamacpp.touch(None)
            return self._server_entry(server)
        m = self._model(model)
        if m and m["runtime"] == "llamacpp":
            if self.llamacpp is None:
                raise ModelNotAllowed("llama.cpp models are not available here")
            await self._make_room("llamacpp")
            server = await self.llamacpp.ensure(model, m["source"], int(num_ctx), m["info"])
            self.llamacpp.touch(None)  # no idle stop while in use
            return self._server_entry(server)
        if m and m["runtime"] == "sdcpp":
            raise ModelNotAllowed(f"{model} makes images; it does not chat")
        await self._make_room("ollama")
        for entry in await self.ollama.ps():
            if entry.get("name") == model or entry.get("model") == model:
                size, vram = int(entry.get("size") or 0), int(entry.get("size_vram") or 0)
                if entry.get("context_length") == num_ctx and size > 0 and vram >= size:
                    return entry
        options = {"num_ctx": int(num_ctx), "num_gpu": 999, "num_thread": OLLAMA_THREADS}
        ka = self.keep_alive if keep_alive is None else keep_alive
        info = self.registry.get(model) if self.registry else None
        if info and info.get("role") == "embed":
            await self.ollama.embed(model, [], options, ka)  # empty input: load only
        else:
            async for _ in self.ollama.chat({"model": model, "messages": [], "options": options, "keep_alive": ka}):
                pass
        entry = await self._guard(model)
        if entry.get("context_length") not in (None, num_ctx):
            raise ResidencyError(f"{model} loaded with context {entry.get('context_length')}, not {num_ctx}")
        return entry

    async def media_route(self, model: str, num_ctx: int) -> dict:
        """How to run Ollama model `model` for a turn with images or audio: Ollama would put the encoder
        on the CPU (llamacpp.py, "Ollama models with images or audio"), so baabaa runs the same files
        with Ollama's own server program itself. {source, facts, ctx}; ModelNotAllowed when it can't."""
        from . import gguf
        from .llamacpp import ollama_engine, ollama_files
        m = self._model(model) or {}
        key = (model, m.get("digest"))
        if key in self._media:
            return self._media[key]
        if self.llamacpp is None:
            raise ModelNotAllowed(f"{model} can't take images or audio here: baabaa's llama.cpp runtime is off")
        engine = ollama_engine(self.gpu.cuda_driver() if hasattr(self.gpu, "cuda_driver") else None)
        if engine is None:
            raise ModelNotAllowed(
                f"{model} can't take images or audio here: Ollama would run the image/audio encoder on the CPU, "
                "and baabaa did not find Ollama's server program to run it on the GPU itself")
        files = ollama_files(await self.ollama.show(model))
        if not files or not files.get("mmproj"):
            raise ModelNotAllowed(f"{model} has no image or audio encoder that baabaa can load")
        facts = await asyncio.to_thread(gguf.describe, files["model"])
        encoder = facts.get("encoder_bytes") or 0
        if files["mmproj"] != files["model"]:
            encoder = os.path.getsize(files["mmproj"])
        # the largest context from the model's fit test that leaves room for the encoder and its work
        fit = m.get("fit") or {}
        total = fit.get("vram_total") or (self.gpu.memory() or {}).get("total") or 0
        ctx = min(4096, int(num_ctx))
        for step in fit.get("steps") or []:
            if (step.get("fits") and step.get("vram_used") and step["ctx"] <= num_ctx
                    and step["vram_used"] + encoder + 768 * 2**20 <= total):
                ctx = max(ctx, int(step["ctx"]))
        route = {"source": {**engine, **files}, "facts": facts, "ctx": ctx}
        self._media[key] = route
        return route

    def _running_media(self, model: str, messages: list, tools, options) -> dict | None:
        """A text-only request for an Ollama model that baabaa is running itself for media right now: it
        uses that server when the prompt fits its context, instead of reloading the model in Ollama (and
        back again at the next turn with an image)."""
        m = self._model(model) or {}
        route = self._media.get((model, m.get("digest")))
        server = self.llamacpp.current if self.llamacpp else None
        if route is None or server is None or not server.alive or server.name != model or server.num_ctx != route["ctx"]:
            return None
        chars = sum(len(str(x.get("content") or "")) for x in messages) + len(json.dumps(tools or []))
        need = chars / 3 + int((options or {}).get("num_predict") or 1024)  # 3 characters a token errs long
        return route if need < route["ctx"] * 0.9 else None

    async def _ollama_file(self, model: str) -> str | None:
        """The model file an Ollama model loads (its runner's command line names it), or None."""
        from .llamacpp import ollama_files
        m = self._model(model) or {}
        key = (model, m.get("digest"))
        if key not in self._files:
            try:
                self._files[key] = ollama_files(await self.ollama.show(model)) or {}
            except (OllamaError, OSError, asyncio.TimeoutError):
                return None
        return self._files[key].get("model")

    async def _cpu_watch(self, model: str, runtime: str) -> cpuwatch.CpuWatch:
        """Watch the CPU use of the program serving this request (cpuwatch.py). Tripping stops the program,
        or for Ollama unloads the model and ends this request, which stops Ollama's work on it."""
        def server_pids(mgr):
            s = mgr.current if mgr is not None else None
            return [s.proc.pid] if s is not None and s.proc is not None and s.proc.returncode is None else []

        if runtime == "sdcpp":
            pids, stop = (lambda: server_pids(self.sdcpp)), self.sdcpp.stop
        elif runtime == "llamacpp":
            pids, stop = (lambda: server_pids(self.llamacpp)), self.llamacpp.stop
        else:
            victim, model_file = asyncio.current_task(), await self._ollama_file(model)
            pids = lambda: cpuwatch.ollama_runner_pids(model_file)  # noqa: E731

            async def stop():
                if victim is not None:
                    victim.cancel()  # closing the connection ends Ollama's work on this request
                try:
                    await self.ollama.unload(model)
                except (OllamaError, OSError, asyncio.TimeoutError):
                    pass

        async def trip(reason):
            await stop()
        return cpuwatch.CpuWatch(pids, trip, self.cpu_limit, self.cpu_window, self.cpu_interval).start()

    async def _ensure_media(self, model: str, media: dict):
        from .llamacpp import LlamaCppError
        while True:
            try:
                return await self.llamacpp.ensure(model, media["source"], media["ctx"], media["facts"])
            except LlamaCppError as exc:
                low = str(exc).lower()
                if media["ctx"] <= 4096 or not any(w in low for w in ("out of memory", "failed to allocate", "cudamalloc")):
                    raise
                media["ctx"] //= 2  # remembered for the next turn with media

    @staticmethod
    def _server_entry(server) -> dict:
        return {"name": server.name, "model": server.name, "size": server.facts.get("weight_bytes"),
                "size_vram": server.load.get("vram_bytes"), "context_length": server.num_ctx, "load": dict(server.load)}

    async def _unload_ollama_all(self) -> None:
        try:
            for entry in await self.ollama.ps():
                await self.ollama.unload(entry.get("name") or entry.get("model"))
        except (OllamaError, OSError, asyncio.TimeoutError):
            pass  # Ollama is not running, so it holds nothing

    async def check_residency(self, model: str, via: str | None = None) -> dict:
        """Return the /api/ps entry for `model`; raise ResidencyError unless it is fully in VRAM.
        `via="llamacpp"`: an Ollama model that baabaa runs itself for a turn with media."""
        m = self._model(model)
        if via == "llamacpp" or (m and m["runtime"] == "llamacpp"):
            server = self.llamacpp.current if self.llamacpp else None
            if server is None or server.name != model or not server.alive:
                raise ResidencyError(f"{model} is not loaded")
            await server.verify()
            return self._server_entry(server)
        for entry in await self.ollama.ps():
            if entry.get("name") == model or entry.get("model") == model:
                size, vram = int(entry.get("size") or 0), int(entry.get("size_vram") or 0)
                if size <= 0 or vram < size:
                    raise ResidencyError(
                        f"{model} is not fully on the GPU ({vram / 2**30:.2f} of {size / 2**30:.2f} GiB in VRAM)")
                return entry
        raise ResidencyError(f"{model} is not loaded")

    def exclusive(self, ticket: "Ticket"):
        """`async with gateway.exclusive(ticket):` holds the GPU for several calls (fit tests)."""
        gateway = self

        class _Hold:
            async def __aenter__(self):
                await gateway.queue.acquire(ticket)
                gateway.sampler.start()
                return ticket

            async def __aexit__(self, *exc):
                gateway.queue.release(ticket)
                gateway.sampler.stop()
                return False

        return _Hold()

    async def chat(self, *, model: str, messages: list, kind: str = "chat", tools=None, think=None,
                   fmt=None, options: dict | None = None, num_ctx: int | None = None,
                   account_id=None, conv_id=None, msg_id=None, window=None, label="", allow_unapproved=False,
                   held: "Ticket | None" = None, keep_alive=None):
        """Async generator of Ollama chat chunks, wrapped in the queue, guard and statistics.

        `held`: the caller already holds the GPU through `exclusive()`; skip the queue.
        """
        if num_ctx is None:
            if self.registry is None:
                raise ModelNotAllowed("no model registry")
            num_ctx = self.registry.num_ctx(model, allow_unapproved=allow_unapproved)
        ticket = held or Ticket(kind=kind, model=model, account_id=account_id, conv_id=conv_id, label=label)
        req = {
            "model": model,
            "messages": messages,
            "options": {**(options or {}), "num_ctx": int(num_ctx), "num_gpu": 999, "num_thread": OLLAMA_THREADS},
            "keep_alive": self.keep_alive if keep_alive is None else keep_alive,
            "shift": False,
            "truncate": False,
        }
        if tools:
            req["tools"] = tools
        if think is not None:
            req["think"] = think
        if fmt is not None:
            req["format"] = fmt

        media = None
        if self.runtime(model) == "ollama" and not MAC:  # macOS: Ollama runs the encoder on the GPU (Metal) itself
            if any(msg.get("images") for msg in messages):
                media = await self.media_route(model, int(num_ctx))
            else:
                media = self._running_media(model, messages, tools, options)
        rec = {"account_id": account_id, "conv_id": conv_id, "msg_id": msg_id, "window": window, "kind": kind,
               "model": model, "num_ctx": int(media["ctx"] if media else num_ctx),
               "think": None if think is None else str(think)}
        t_queue = mono_ms()
        if held is None:
            await self.queue.acquire(ticket)
        t_start = mono_ms()
        rec["queue_wait_ms"] = round(t_start - t_queue)
        energy0 = self.gpu.energy_mj()
        if held is None:
            self.sampler.start()
        outcome, error, gaps, last, first = "error", None, [], None, None
        final = {}
        stream = None
        watch = None
        t_loaded = t_start
        llama = media is not None or self.runtime(model) == "llamacpp"
        via = "llamacpp" if llama else None
        try:
            loaded = await self.ensure_loaded(model, int(num_ctx), keep_alive, media=media)
            rec["model_bytes"], rec["vram_bytes"] = loaded.get("size"), loaded.get("size_vram")
            t_loaded = mono_ms()
            if t_loaded - t_start > 50:
                rec["preload_ms"] = round(t_loaded - t_start)
            watch = await self._cpu_watch(model, "llamacpp" if llama else "ollama")
            stream = self.llamacpp.current.chat(req) if llama else self._ollama_chat(req, model, account_id)
            if not llama and harmony.reads_raw((self._model(model) or {}).get("info") or {}):
                stream = harmony.chunks(stream)  # a harmony reply Ollama passes through unread
            async for chunk in stream:
                now = mono_ms()
                msg = chunk.get("message") or {}
                produced = bool(msg.get("content") or msg.get("thinking") or msg.get("tool_calls"))
                if first is None:
                    first = now
                    rec["ttft_ms"] = round(now - t_loaded)
                    entry = await self._guard(model, via)
                    rec["residency_ok"] = 1
                    rec["model_bytes"] = entry.get("size")
                    rec["vram_bytes"] = entry.get("size_vram")
                if produced:
                    if last is not None and len(gaps) < 20000:
                        gaps.append(round(now - last))
                    last = now
                if chunk.get("done"):
                    final = chunk
                yield chunk
            if watch is not None and watch.tripped:  # the guard stopped the server; its stream just ended
                raise ResidencyError(f"{model}: {watch.tripped}")
            if not final:
                outcome, error = "incomplete", "stream ended without a final chunk"
            else:
                outcome = "ok" if final.get("done_reason") in (None, "stop", "") else final.get("done_reason")
        except asyncio.CancelledError:
            if watch is not None and watch.tripped:  # the CPU guard ended this request
                task = asyncio.current_task()
                if task is not None and hasattr(task, "uncancel"):
                    task.uncancel()
                outcome, error, rec["residency_ok"] = "cpu", watch.tripped, 0
                raise ResidencyError(f"{model}: {watch.tripped}") from None
            outcome = "stopped"
            raise
        except GeneratorExit:
            outcome = "stopped"
            raise
        except ResidencyError as exc:
            outcome, error = ("cpu" if watch is not None and watch.tripped else "residency"), str(exc)
            rec["residency_ok"] = 0
            raise
        except Exception as exc:
            if watch is not None and watch.tripped:  # the guard stopped the server under the stream
                outcome, error, rec["residency_ok"] = "cpu", watch.tripped, 0
                raise ResidencyError(f"{model}: {watch.tripped}") from exc
            if isinstance(exc, OllamaError):
                error = str(exc)
                if first is not None and not llama and await self._freezes_after_errors():
                    await self._unload_quietly(model)  # it failed mid-answer, after which it can stay stuck on the model
                raise
            if isinstance(exc, (ConnectionError, OSError)):
                reason = str(exc) or type(exc).__name__
                error = f"connection: {reason}"
                raise OllamaError(f"Ollama connection failed: {reason}") from exc
            raise
        finally:
            if watch is not None:
                await watch.stop()
                rec["cpu_s"], rec["cpu_peak"] = round(watch.cpu_s, 2), round(watch.peak, 2)
            if stream is not None:
                try:
                    await stream.aclose()  # closes the connection now, which stops generation
                except (RuntimeError, OllamaError, OSError):
                    pass
            if llama and self.llamacpp is not None:
                from .llamacpp import keep_alive_seconds
                self.llamacpp.touch(keep_alive_seconds(self.keep_alive if keep_alive is None else keep_alive))
            if held is None:
                self.queue.release(ticket)
                self.sampler.stop()
            energy1 = self.gpu.energy_mj()
            wall = mono_ms() - t_start
            ns = 1_000_000
            rec.update({
                "outcome": outcome,
                "error": error,
                "wall_ms": round(wall),
                "load_ms": rec.pop("preload_ms", None) or _ms(final.get("load_duration"), ns),
                "prompt_tokens": final.get("prompt_eval_count"),
                "output_tokens": final.get("eval_count"),
                "prompt_eval_ms": _ms(final.get("prompt_eval_duration"), ns),
                "eval_ms": _ms(final.get("eval_duration"), ns),
                "total_ms": _ms(final.get("total_duration"), ns) or round(wall),
                "energy_mj": (energy1 - energy0) if (energy0 is not None and energy1 is not None) else None,
                "gaps": gaps or None,
            })
            if (final.get("eval_count") or 0) >= 2 and final.get("eval_duration"):  # one token has no speed
                rec["tokens_per_s"] = round(final["eval_count"] / (final["eval_duration"] / 1e9), 2)
            self.stats.model_request(**rec)

    async def _ollama_chat(self, req: dict, model: str, account_id=None):
        """Ollama's chat stream, watched until it starts answering. When Ollama is stuck on the model (see
        STUCK_IDLE_S), the model is unloaded and the request sent once more."""
        for attempt in (1, 2):
            stream = self.ollama.chat(req)
            nxt = asyncio.ensure_future(stream.__anext__())
            try:
                stuck = await self._stuck_before_first(nxt)
            except BaseException:  # stopped by the person, or the server shutting down
                nxt.cancel()
                await asyncio.wait({nxt})
                await stream.aclose()
                raise
            if stuck:
                nxt.cancel()
                await asyncio.wait({nxt})
                await stream.aclose()
                self.stats.event("ollama_stuck", account_id, model=model, attempt=attempt)
                await self._unload_quietly(model)
                if attempt == 1:
                    continue
                raise OllamaError("Ollama stopped answering for this model. baabaa unloaded the model and asked again, "
                                  "but it still did not answer. Restarting Ollama fixes this: "
                                  + ("quit Ollama from the menu bar and open it again" if MAC else "sudo systemctl restart ollama"))
            try:
                try:
                    yield nxt.result()
                except StopAsyncIteration:
                    return
                async for chunk in stream:
                    yield chunk
            finally:
                await stream.aclose()
            return

    async def _stuck_before_first(self, nxt) -> bool:
        """Wait for Ollama's first chunk; True when it looks stuck rather than busy."""
        idle = waited = 0.0
        while True:
            done, _ = await asyncio.wait({nxt}, timeout=STUCK_CHECK_S)
            if done:
                return False
            waited += STUCK_CHECK_S
            reading = getattr(self.gpu, "utilization", None)
            util = ((reading() if reading else None) or {}).get("gpu")
            idle = idle + STUCK_CHECK_S if util is not None and util < 10 else 0.0
            if idle >= STUCK_IDLE_S or (util is None and waited >= STUCK_BLIND_S):
                return True

    async def _freezes_after_errors(self) -> bool:
        """True when this Ollama can stay stuck on a model after a failed reply (older than FREEZE_FIXED, or unknown)."""
        version, at = self._version
        if mono_ms() - at > VERSION_TTL_MS:
            try:
                version = library.version_tuple(await self.ollama.version())
            except (OllamaError, OSError, asyncio.TimeoutError):
                version = ()
            self._version = (version, mono_ms())
        return not version or version < FREEZE_FIXED

    async def _unload_quietly(self, model: str) -> None:
        try:
            await self.ollama.unload(model)
        except (OllamaError, OSError, asyncio.TimeoutError):
            pass

    async def _guard(self, model: str, via: str | None = None) -> dict:
        try:
            return await self.check_residency(model, via)
        except ResidencyError:
            try:
                await self.ollama.unload(model)
            except OllamaError:
                pass
            raise

    async def embed(self, model: str, inputs: list[str], account_id=None, window=None, conv_id=None) -> list:
        num_ctx = self.registry.num_ctx(model)
        ticket = Ticket(kind="embed", model=model, account_id=account_id, conv_id=conv_id, label="embeddings",
                        background=window == "background")
        t_queue = mono_ms()
        await self.queue.acquire(ticket)
        t_start = mono_ms()
        energy0 = self.gpu.energy_mj()
        outcome, error = "error", None
        try:
            await self.ensure_loaded(model, num_ctx)
            vectors = await self.ollama.embed(model, inputs, {"num_ctx": num_ctx, "num_gpu": 999, "num_thread": OLLAMA_THREADS},
                                              self.keep_alive)
            outcome = "ok"
            return vectors
        except asyncio.CancelledError:
            outcome = "stopped"
            raise
        except (OllamaError, ResidencyError) as exc:
            error = str(exc)
            raise
        finally:
            self.queue.release(ticket)
            energy1 = self.gpu.energy_mj()
            self.stats.model_request(
                account_id=account_id, window=window, kind="embed", model=model, num_ctx=num_ctx,
                queue_wait_ms=round(t_start - t_queue), wall_ms=round(mono_ms() - t_start),
                total_ms=round(mono_ms() - t_start), prompt_tokens=None, outcome=outcome, error=error,
                energy_mj=(energy1 - energy0) if (energy0 is not None and energy1 is not None) else None)

    async def image(self, model: str, req: dict, account_id=None, conv_id=None, msg_id=None, window=None,
                    label: str = "making an image", held: "Ticket | None" = None, kind: str = "image",
                    keep_alive=None, on_progress=None) -> dict:
        """Make an image with an approved image model (or one under test, when `held`). Returns
        {'images': [png bytes], 'seed', 'seconds', 'load'}; statistics like any model request."""
        m = self._model(model)
        if m is None or m["runtime"] != "sdcpp" or self.sdcpp is None:
            raise ModelNotAllowed(f"{model} is not an image model here")
        if held is None and not m["approved"]:
            raise ModelNotAllowed(f"{model} has not passed the GPU fit test and been approved")
        ticket = held or Ticket(kind=kind, model=model, account_id=account_id, conv_id=conv_id, label=label)
        t_queue = mono_ms()
        if held is None:
            await self.queue.acquire(ticket)
        t_start = mono_ms()
        energy0 = self.gpu.energy_mj()
        self.sampler.start()
        outcome, error, rec = "error", None, {}
        watch = None
        try:
            await self._make_room("sdcpp")
            t_load = mono_ms()
            server = await self.sdcpp.ensure(model, m["source"])
            self.sdcpp.touch(None)
            rec["load_ms"] = round(mono_ms() - t_load) if mono_ms() - t_load > 50 else None
            load = await server.verify()
            d = m["source"].get("defaults") or {}
            full = {**{k: v for k, v in d.items()}, **{k: v for k, v in req.items() if v is not None}}
            watch = await self._cpu_watch(model, "sdcpp")
            res = await server.generate(full, on_progress)
            res["load"] = dict(load)
            outcome = "ok"
            rec.update({"model_bytes": load.get("weights"), "vram_bytes": load.get("vram_bytes")})
            return res
        except asyncio.CancelledError:
            outcome = "stopped"
            raise
        except ResidencyError as exc:
            outcome, error = "residency", str(exc)
            raise
        except Exception as exc:
            if watch is not None and watch.tripped:
                outcome, error = "cpu", watch.tripped
                raise ResidencyError(f"{model}: {watch.tripped}") from exc
            error = str(exc)
            raise
        finally:
            if watch is not None:
                await watch.stop()
                rec["cpu_s"], rec["cpu_peak"] = round(watch.cpu_s, 2), round(watch.peak, 2)
            if self.sdcpp is not None:
                from .llamacpp import keep_alive_seconds
                self.sdcpp.touch(keep_alive_seconds(self.keep_alive if keep_alive is None else keep_alive))
            self.sampler.stop()
            if held is None:
                self.queue.release(ticket)
            energy1 = self.gpu.energy_mj()
            self.stats.model_request(account_id=account_id, conv_id=conv_id, msg_id=msg_id, window=window, kind=kind,
                                     model=model, num_ctx=0, queue_wait_ms=round(t_start - t_queue),
                                     wall_ms=round(mono_ms() - t_start), total_ms=round(mono_ms() - t_start),
                                     outcome=outcome, error=error,
                                     energy_mj=(energy1 - energy0) if (energy0 is not None and energy1 is not None) else None,
                                     **{k: v for k, v in rec.items() if v is not None})

    async def vector_search(self, matrix: bytes, n: int, d: int, query: list[float], k: int,
                            account_id=None, conv_id=None, window=None) -> list[tuple[int, float]]:
        """Top-k rows of a float32 matrix by dot product with `query`, computed on the GPU (see cuda.py).

        Runs in its own worker process, inside the GPU queue. Raises GpuSearchError; never falls back to the CPU.
        """
        if n == 0:
            return []
        ticket = Ticket(kind="search", model="gpu-search", account_id=account_id, conv_id=conv_id, label="searching knowledge")
        t_queue = mono_ms()
        await self.queue.acquire(ticket)
        t_start = mono_ms()
        energy0 = self.gpu.energy_mj()
        proc, outcome, error = None, "error", None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-s", CUDA_WORKER, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            payload = json.dumps({"n": n, "d": d, "k": k}).encode() + b"\n" + matrix + array.array("f", query).tobytes()
            out, err = await asyncio.wait_for(proc.communicate(payload), timeout=60)
            try:
                res = json.loads(out.decode() or "{}")
            except ValueError:
                res = {"error": (err.decode(errors="replace").strip() or "no output")[-300:]}
            if proc.returncode != 0 or "error" in res:
                error = res.get("error") or f"exit code {proc.returncode}"
                raise GpuSearchError(error)
            outcome = "ok"
            return [(int(i), float(s)) for i, s in res["top"]]
        except asyncio.TimeoutError as exc:
            error = "timed out"
            raise GpuSearchError("GPU search timed out") from exc
        except asyncio.CancelledError:
            outcome = "stopped"
            raise
        except OSError as exc:
            error = str(exc)
            raise GpuSearchError(str(exc)) from exc
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            self.queue.release(ticket)
            energy1 = self.gpu.energy_mj()
            self.stats.event("gpu_search", account_id, conv_id, window, rows=n, dim=d, k=k, outcome=outcome, error=error,
                             queue_wait_ms=round(t_start - t_queue), wall_ms=round(mono_ms() - t_start),
                             energy_mj=(energy1 - energy0) if (energy0 is not None and energy1 is not None) else None)

    async def unload(self, model: str) -> None:
        if self.runtime(model) == "sdcpp":
            server = self.sdcpp.current if self.sdcpp else None
            if server is not None and server.name == model:
                await self.sdcpp.stop()
            return
        server = self.llamacpp.current if self.llamacpp else None
        if server is not None and server.name == model:  # a llama.cpp model, or an Ollama model run for media
            await self.llamacpp.stop()
        if self.runtime(model) == "llamacpp":
            return
        try:
            await self.ollama.unload(model)
        except OllamaError:
            pass

    async def complete(self, **kw) -> dict:
        """Run a chat request to completion; return {'content', 'thinking', 'tool_calls', 'final'}."""
        content, thinking, calls, final = [], [], [], {}
        async for chunk in self.chat(**kw):
            m = chunk.get("message") or {}
            if m.get("content"):
                content.append(m["content"])
            if m.get("thinking"):
                thinking.append(m["thinking"])
            if m.get("tool_calls"):
                calls.extend(m["tool_calls"])
            if chunk.get("done"):
                final = chunk
        return {"content": "".join(content), "thinking": "".join(thinking), "tool_calls": calls, "final": final}


def _ms(value, ns):
    return round(value / ns) if value else None
