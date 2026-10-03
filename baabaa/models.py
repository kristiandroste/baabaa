"""Models: the registry of installed models, the GPU fit test, installs, and "Check for newer models".

Rules enforced here:
- a model is offered only after a fit test proves it loads 100% into VRAM, and the owner approves it;
- each model runs at one fixed context size, chosen by the fit test (changing it would force a reload);
- weights that cannot fit in VRAM are never loaded, not even for a test.
"""

import asyncio
import json
import os
import re
import sys

from . import db, library
from .gateway import ModelNotAllowed, ResidencyError, Ticket
from .ollama import OllamaError
from .util import clip, dumps, loads, mono_ms, new_id, now_ms

FIT_STEPS = [4096, 8192, 16384, 32768, 49152, 65536, 98304, 131072, 196608, 262144]
DEFAULT_MAX_CTX = 65536          # the default context cap for chat models (the fit test may allow more)
VRAM_RESERVE = 512 * 2**20       # keep this much VRAM free at the chosen context
SPEED_PROMPT = "Describe, in about eighty words, how a flock of sheep moves across a hillside."

# The tool check in the fit test: does the model call a tool when it must, answer without one when it should,
# and write a long argument (a small web page) that Ollama can read? Small models fail these in real use.
_WEATHER = {"type": "function", "function": {
    "name": "get_weather", "description": "The current weather in a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string", "description": "The city"}}, "required": ["city"]}}}
_SAVE = {"type": "function", "function": {
    "name": "save_file", "description": "Save a text file.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "File name"},
                                                    "content": {"type": "string", "description": "The complete content"}},
                   "required": ["path", "content"]}}}
TOOL_CHECKS = [
    ("calls", [_WEATHER], "What is the weather in Oslo right now?",
     lambda r: any(c["name"] == "get_weather" and "oslo" in str(c["args"].get("city", "")).lower() for c in r["calls"])),
    ("answers", [_WEATHER], "What is 12 times 12? Reply with the number only.",
     lambda r: not r["calls"] and "144" in r["content"]),
    ("long_argument", [_SAVE], "Save page.html: a complete small HTML page titled Flock, with an h1 heading and three "
     "paragraphs about sheep.",
     lambda r: any(c["name"] == "save_file" and "<h1" in str(c["args"].get("content", "")).lower() for c in r["calls"])),
]
TOOL_CHECK_SYSTEM = "You are a helpful assistant. You can call tools; call them with proper tool calls."


def _param_b(model: dict) -> float | None:
    """Parameter size in billions: from the tag label ('qwen3.5:9b' -> 9) or Ollama's details."""
    tag = model["name"].split(":", 1)[1] if ":" in model["name"] else ""
    label = re.match(r"^(e?\d+(?:\.\d+)?[bm])", tag, re.I)
    if label:
        size = library.size_to_b(label.group(1))
        if size:
            return size
    ps = (model.get("info") or {}).get("parameter_size") or ""
    m = re.match(r"([\d.]+)\s*([BM])", ps, re.I)
    if m:
        value = float(m.group(1))
        return value / 1000 if m.group(2).upper() == "M" else value
    return None


class ModelRegistry:
    def __init__(self, maindb, ollama, events=None):
        self.maindb = maindb
        self.con = maindb.con
        self.ollama = ollama
        self.events = events

    # queries ------------------------------------------------------------------------------------
    def all(self) -> list[dict]:
        return [self._row(r) for r in db.rows(self.con.execute("SELECT * FROM models ORDER BY name"))]

    def get(self, name: str) -> dict | None:
        r = db.row(self.con.execute("SELECT * FROM models WHERE name=?", (name,)))
        return self._row(r) if r else None

    def approved(self, role: str = "chat") -> list[dict]:
        return [m for m in self.all() if m["approved"] and m["role"] == role and (m["num_ctx"] or role == "image")]

    def audio_model(self) -> str | None:
        """The approved model that transcribes speech: the owner's choice, else the smallest that hears audio."""
        audio = [m for m in self.approved("chat") if "audio" in (m["info"].get("capabilities") or [])]
        preferred = self.maindb.get_setting("stt_model")
        for m in audio:
            if m["name"] == preferred:
                return m["name"]
        return min(audio, key=lambda m: m["info"].get("size") or 0)["name"] if audio else None

    def image_model(self, name: str | None = None) -> str | None:
        """`name` if approved, else the owner's default, else the fastest approved image model (by its fit test)."""
        models = self.approved("image")
        names = [m["name"] for m in models]
        if name in names:
            return name
        preferred = self.maindb.get_setting("default_image_model")
        if preferred in names:
            return preferred
        timed = lambda m: m["fit"].get("seconds_1024") or 4 * (m["fit"].get("seconds_512") or 1e6)  # noqa: E731
        return min(models, key=timed)["name"] if models else None

    def num_ctx(self, name: str, allow_unapproved: bool = False) -> int:
        m = self.get(name)
        if m is None:
            raise ModelNotAllowed(f"{name} is not installed")
        if m["approved"] and m["num_ctx"]:
            return m["num_ctx"]
        if allow_unapproved and m["fit"].get("fits"):
            return m["fit"]["num_ctx"]
        raise ModelNotAllowed(f"{name} has not passed the GPU fit test and been approved")

    def default_chat(self) -> str | None:
        preferred = self.maindb.get_setting("default_model")
        approved = self.approved("chat")
        names = [m["name"] for m in approved]
        if preferred in names:
            return preferred
        return names[0] if names else None

    def embed_model(self) -> str | None:
        approved = self.approved("embed")
        return approved[0]["name"] if approved else None

    def _row(self, r: dict) -> dict:
        r["fit"] = loads(r["fit"], {})
        r["info"] = loads(r["info"], {})
        r["source"] = loads(r.get("source"), {})
        r["approved"] = bool(r["approved"])
        r["param_b"] = _param_b(r)
        return r

    # changes ------------------------------------------------------------------------------------
    async def sync(self) -> list[dict]:
        """Refresh the table from Ollama's installed models. A changed digest resets the fit test."""
        tags = await self.ollama.tags()
        installed = set()
        for t in tags:
            name = t["name"]
            installed.add(name)
            existing = self.get(name)
            if existing and existing["digest"] == t.get("digest") and existing["info"].get("capabilities") is not None:
                continue
            try:
                show = await self.ollama.show(name)
            except OllamaError:
                show = {}
            caps = show.get("capabilities") or []
            info = _info(t, show)
            role = "embed" if "embedding" in caps and "completion" not in caps else "chat"
            now = now_ms()
            if existing is None:
                self.con.execute(
                    "INSERT INTO models(name, digest, role, info, updated_ms) VALUES (?,?,?,?,?)",
                    (name, t.get("digest"), role, dumps(info), now))
            elif existing["digest"] != t.get("digest"):
                self.con.execute(
                    "UPDATE models SET digest=?, role=?, info=?, fit='{}', approved=0, num_ctx=NULL, updated_ms=? WHERE name=?",
                    (t.get("digest"), role, dumps(info), now, name))
            else:
                self.con.execute("UPDATE models SET role=?, info=?, updated_ms=? WHERE name=?", (role, dumps(info), now, name))
        for m in self.all():
            if m["runtime"] == "ollama" and m["name"] not in installed:
                self.con.execute("DELETE FROM models WHERE name=?", (m["name"],))
        self._changed()
        return self.all()

    def add_llamacpp(self, name: str, source: dict, role: str = "chat") -> dict:
        """Register a model served by a llama.cpp server program. Its files stay where they are."""
        from . import gguf
        if sys.platform == "darwin":
            raise ValueError("Models that run outside Ollama need Linux with an NVIDIA GPU for now; on a Mac, baabaa "
                             "runs Ollama's models.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,62}", name or ""):
            raise ValueError("Name the model with lower-case letters, digits, '.', '_' or '-' (no ':', which Ollama uses)")
        for key in ("server", "model"):
            path = source.get(key) or ""
            if not os.path.isabs(path) or not os.path.isfile(path):
                raise ValueError(f"{key}: {path or '(empty)'} is not a file")
        if not os.access(source["server"], os.X_OK):
            raise ValueError(f"{source['server']} is not executable")
        if source.get("mmproj") and not os.path.isfile(source["mmproj"]):
            raise ValueError(f"mmproj: {source['mmproj']} is not a file")
        from .llamacpp import forbidden_args
        bad = forbidden_args(source.get("args"))
        if bad:
            raise ValueError(f"{', '.join(bad)}: baabaa sets GPU placement, context and caching itself, by the GPU rules")
        if self.get(name) is not None:
            raise ValueError(f"There is already a model called {name}")
        facts = gguf.describe(source["model"])
        size_label = facts.get("size_label") or ""
        caps = list(facts["capabilities"])
        if source.get("mmproj"):
            caps.append("vision")
        info = {"size": facts["weight_bytes"], "family": facts["arch"], "parameter_size": size_label,
                "quantization": f"GGUF type {facts['file_type']}", "capabilities": caps,
                "context_length": facts["context_length"], "arch": facts["arch"], "block_count": facts["block_count"],
                "full_attention_interval": facts["full_attention_interval"], "head_count_kv": facts["head_count_kv"],
                "head_dim": facts["head_dim"], "weight_bytes": facts["weight_bytes"],
                "token_embd_bytes": facts["token_embd_bytes"], "file": os.path.basename(source["model"])}
        clean = {"server": source["server"], "model": source["model"],
                 "lib_dirs": [d for d in (source.get("lib_dirs") or []) if d],
                 "args": [str(a) for a in (source.get("args") or [])], "mmproj": source.get("mmproj") or None}
        self.con.execute("INSERT INTO models(name, digest, role, info, runtime, source, updated_ms) VALUES (?,?,?,?,?,?,?)",
                         (name, _file_digest(source["model"]), role, dumps(info), "llamacpp", dumps(clean), now_ms()))
        self._changed()
        return self.get(name)

    def add_sdcpp(self, name: str, source: dict) -> dict:
        """Register an image model served by stable-diffusion.cpp's sd-server. Its files stay where they are."""
        from . import sdcpp
        if sys.platform == "darwin":
            raise ValueError("Models that run outside Ollama need Linux with an NVIDIA GPU for now; on a Mac, baabaa "
                             "runs Ollama's models.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,62}", name or ""):
            raise ValueError("Name the model with lower-case letters, digits, '.', '_' or '-'")
        if self.get(name) is not None:
            raise ValueError(f"There is already a model called {name}")
        server = source.get("server") or ""
        if not os.path.isabs(server) or not os.path.isfile(server) or not os.access(server, os.X_OK):
            raise ValueError(f"server: {server or '(empty)'} is not an executable file")
        files = {k: source.get(k) for k in sdcpp.FILES if source.get(k)}
        if not files.get("diffusion_model") and not files.get("model"):
            raise ValueError("Give the diffusion model file")
        for k, path in files.items():
            if not os.path.isabs(path) or not os.path.isfile(path):
                raise ValueError(f"{k}: {path} is not a file")
        args = [str(a) for a in (source.get("args") or [])]
        bad = [a for a in args if a.split("=", 1)[0] in sdcpp.CPU_FLAGS]
        if bad:
            raise ValueError(f"{', '.join(bad)}: baabaa places the model's parts itself, by the GPU rules "
                             "(staged=true is the one way to keep weights in system memory, and needs the owner's leave)")
        clean = {"server": server, **files, "lib_dirs": [d for d in (source.get("lib_dirs") or []) if d], "args": args,
                 "staged": bool(source.get("staged")), "edit": bool(source.get("edit")),
                 "defaults": {k: source[k] for k in ("steps", "cfg", "width", "height") if source.get(k) is not None}}
        size = sdcpp.model_bytes(clean)
        caps = ["image"] + (["edit"] if clean["edit"] else [])
        info = {"size": size, "family": "stable-diffusion.cpp", "capabilities": caps, "files": {k: os.path.basename(v) for k, v in files.items()},
                "staged": clean["staged"], "defaults": clean["defaults"]}
        digest = "files:" + ",".join(_file_digest(v) for v in files.values())
        self.con.execute("INSERT INTO models(name, digest, role, info, runtime, source, updated_ms) VALUES (?,?,?,?,?,?,?)",
                         (name, digest[:500], "image", dumps(info), "sdcpp", dumps(clean), now_ms()))
        self._changed()
        return self.get(name)

    def remove_external(self, name: str) -> None:
        m = self.get(name)
        if m is None or m["runtime"] == "ollama":
            raise ValueError(f"{name} is not a model from another program")
        self.con.execute("DELETE FROM models WHERE name=?", (name,))
        self._changed()

    def set_fit(self, name: str, fit: dict) -> None:
        self.con.execute("UPDATE models SET fit=?, updated_ms=? WHERE name=?", (dumps(fit), now_ms(), name))
        m = self.get(name)
        if m and m["approved"]:
            if fit.get("fits"):
                # an approved model keeps the context the owner chose while it still fits
                keep = m["num_ctx"] if m["num_ctx"] and m["num_ctx"] <= (fit.get("max_ctx") or 0) else fit["num_ctx"]
                self.con.execute("UPDATE models SET num_ctx=? WHERE name=?", (keep, name))
            else:
                self.con.execute("UPDATE models SET approved=0, num_ctx=NULL WHERE name=?", (name,))
        self._changed()

    def approve(self, name: str, approved: bool, num_ctx: int | None = None) -> dict:
        m = self.get(name)
        if m is None:
            raise ModelNotAllowed(f"{name} is not installed")
        if approved:
            fit = m["fit"]
            if not fit.get("fits"):
                raise ModelNotAllowed(f"{name} has not passed the GPU fit test")
            if m["role"] == "image":
                self.con.execute("UPDATE models SET approved=1, approved_ms=?, num_ctx=0, updated_ms=? WHERE name=?",
                                 (now_ms(), now_ms(), name))
                self._changed()
                return self.get(name)
            ctx = num_ctx or fit["num_ctx"]
            if ctx > fit.get("max_ctx", 0):
                raise ModelNotAllowed(f"{name} only fits up to {fit.get('max_ctx')} tokens of context")
            self.con.execute("UPDATE models SET approved=1, approved_ms=?, num_ctx=?, updated_ms=? WHERE name=?",
                             (now_ms(), ctx, now_ms(), name))
            if m["role"] == "chat" and not self.maindb.get_setting("default_model"):
                self.maindb.set_setting("default_model", name)  # the first approved chat model is the default
        else:
            self.con.execute("UPDATE models SET approved=0, num_ctx=NULL, updated_ms=? WHERE name=?", (now_ms(), name))
        self._changed()
        return self.get(name)

    def _changed(self) -> None:
        if self.events:
            self.events.broadcast("models.updated", {})


def pull_error(model: str, message: str) -> str | None:
    """What a failed install means, in plain words, from Ollama's message (None when it says nothing better)."""
    m = (message or "").lower()
    if "file does not exist" in m or "manifest unknown" in m or ("manifest" in m and "not found" in m):
        return (f"Ollama's library has no model called {model}. Check the name and tag at ollama.com/library. Models that "
                "run on llama.cpp or stable-diffusion.cpp (GGUF files on this computer) are added under Models from "
                "other programs.")
    if any(k in m for k in ("no such host", "dial tcp", "connection refused", "i/o timeout", "tls handshake",
                            "network is unreachable", "name resolution")):
        return f"Ollama could not reach ollama.com to download {model}. Check this computer's internet connection, then try again."
    if "no space left" in m:
        return f"The disk Ollama keeps its models on is full, so {model} could not be saved. Free some space, then try again."
    if re.search(r"\b(?:401|403|unauthorized|forbidden)\b", m):
        return f"ollama.com refused to send {model}: it may be private or need an ollama.com account. Check the name."
    if "invalid model name" in m or "invalid reference" in m:
        return f"{model} is not a valid model name. Use a name and tag such as qwen3.5:9b."
    if any(k in m for k in ("max retries exceeded", "unexpected eof", "connection reset")):
        return f"The download of {model} kept breaking off. Try again: it continues where it stopped."
    return None


def _file_digest(path: str) -> str:
    """A cheap identity for a model file: size, modification time and the first megabyte's hash."""
    import hashlib
    st = os.stat(path)
    with open(path, "rb") as f:
        head = hashlib.sha256(f.read(2**20)).hexdigest()[:16]
    return f"file:{st.st_size}:{int(st.st_mtime)}:{head}"


def _info(tag: dict, show: dict) -> dict:
    details = tag.get("details") or show.get("details") or {}
    mi = show.get("model_info") or {}
    arch = mi.get("general.architecture") or details.get("family") or ""
    return {
        "size": tag.get("size"),
        "modified_at": tag.get("modified_at"),
        "family": details.get("family"),
        "parameter_size": details.get("parameter_size"),
        "quantization": details.get("quantization_level"),
        "capabilities": show.get("capabilities") or [],
        "context_length": mi.get(f"{arch}.context_length"),
        "arch": arch,
        "block_count": mi.get(f"{arch}.block_count"),
        "full_attention_interval": mi.get(f"{arch}.full_attention_interval"),
        **({"output": "harmony"} if _raw_harmony(show) else {}),
    }


def _raw_harmony(show: dict) -> bool:
    """A harmony-format model whose replies Ollama passes through unread ("PARSER passthrough"): baabaa reads
    them (harmony.py). Ollama's own harmony reader cannot take every such model's output."""
    template = show.get("template") or ""
    parser = re.search(r"^PARSER\s+(\S+)", show.get("modelfile") or "", re.M)
    return bool(parser) and parser.group(1) == "passthrough" and "<|channel|>" in template and "<|message|>" in template


class ModelJobs:
    """Background jobs: fit tests, installs (pulls) and model scouting. Progress goes to owners live."""

    def __init__(self, registry, gateway, ollama, maindb, stats, events, gpu):
        self.registry = registry
        self.gateway = gateway
        self.ollama = ollama
        self.maindb = maindb
        self.stats = stats
        self.events = events
        self.gpu = gpu
        self.tasks: dict[str, asyncio.Task] = {}

    # plumbing -----------------------------------------------------------------------------------
    def _start(self, kind: str, account_id: str | None, params: dict, coro_fn) -> str:
        job_id = new_id()
        self.maindb.job_create(job_id, kind, account_id, params)
        task = asyncio.get_running_loop().create_task(self._wrap(job_id, kind, params, coro_fn))
        self.tasks[job_id] = task
        self._publish(job_id)
        return job_id

    async def _wrap(self, job_id, kind, params, coro_fn):
        started = mono_ms()
        try:
            result = await coro_fn(job_id)
            self.maindb.job_update(job_id, status="done", result=result or {})
            outcome = "ok"
        except asyncio.CancelledError:
            self.maindb.job_update(job_id, status="cancelled")
            outcome = "cancelled"
        except Exception as exc:  # a job failure is reported, never raised into the server
            self.maindb.job_update(job_id, status="error", error=str(exc)[:500])
            outcome = "error"
        finally:
            self.tasks.pop(job_id, None)
            job = self.maindb.job_get(job_id)
            result = {k: v for k, v in (job.get("result") or {}).items() if k != "query"}  # what the owner typed is content
            self.stats.job(id=job_id, account_id=job.get("account_id"), kind=kind, model=params.get("model"),
                           duration_ms=round(mono_ms() - started), bytes=(job.get("progress") or {}).get("total"),
                           outcome=outcome, data={"result": result or None, "error": job.get("error")})
            self._publish(job_id)

    def _progress(self, job_id: str, progress: dict) -> None:
        self.maindb.job_update(job_id, progress=progress)
        self._publish(job_id)

    def _publish(self, job_id: str) -> None:
        job = self.maindb.job_get(job_id)
        if job:
            self.events.publish(job.get("account_id"), "job", job, owners=True)

    def cancel(self, job_id: str) -> bool:
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
            return True
        return False

    def running(self, kind: str, model: str | None = None) -> str | None:
        for job_id in self.tasks:
            job = self.maindb.job_get(job_id)
            if job and job["kind"] == kind and (model is None or job["params"].get("model") == model):
                return job_id
        return None

    # fit test -----------------------------------------------------------------------------------
    def start_fit(self, model: str, account_id: str | None) -> str:
        existing = self.running("fit", model)
        if existing:
            return existing
        return self._start("fit", account_id, {"model": model}, lambda job_id: self._fit(job_id, model))

    async def _fit(self, job_id: str, model: str) -> dict:
        try:
            await self.registry.sync()
        except (OllamaError, OSError, asyncio.TimeoutError):
            if (self.registry.get(model) or {}).get("runtime", "ollama") == "ollama":
                raise
        m = self.registry.get(model)
        if m is not None and m["runtime"] == "sdcpp":
            return await self._fit_image(job_id, m)
        if m is None:
            raise ModelNotAllowed(f"{model} is not installed")
        info = m["info"]
        mem = self.gpu.memory()
        total = mem["total"] if mem else None
        size = info.get("size") or 0
        base = {"tested_ms": now_ms(), "native_ctx": info.get("context_length"), "vram_total": total, "weights": size}
        if total and size > total - VRAM_RESERVE:
            fit = {**base, "fits": False, "reason": "The weights alone are larger than the GPU's memory.", "steps": []}
            self.registry.set_fit(model, fit)
            return fit
        native = int(info.get("context_length") or 8192)
        if m["role"] == "embed":
            steps = [min(native, 8192)]
        else:
            steps = [s for s in FIT_STEPS if s <= native] or [native]
        max_ctx_setting = int(self.maindb.get_setting("max_default_ctx", DEFAULT_MAX_CTX))
        results = []
        ticket = Ticket(kind="fit", model=model, label=f"GPU fit test: {model}")
        async with self.gateway.exclusive(ticket):
            for ctx in steps:
                self._progress(job_id, {"phase": "loading", "ctx": ctx, "steps": results, "of": len(steps)})
                t0 = mono_ms()
                try:
                    entry = await self.gateway.ensure_loaded(model, ctx, keep_alive="5m")
                except (ResidencyError, OllamaError) as exc:
                    results.append({"ctx": ctx, "fits": False, "error": str(exc)[:300]})
                    break
                mem = self.gpu.memory() or {}
                step = {"ctx": ctx, "fits": True, "size": entry.get("size"), "load_ms": round(mono_ms() - t0),
                        "vram_used": mem.get("used"), "vram_free": mem.get("free")}
                if entry.get("load"):  # a llama.cpp server: its own VRAM and RAM readings
                    step["server"] = {k: entry["load"].get(k) for k in ("vram_bytes", "rss_anon_bytes", "ram", "offloaded",
                                                                          "buffers_mib", "kv_mib", "rs_mib", "encoder", "load_ms")
                                      if entry["load"].get(k) is not None}
                results.append(step)
                # Low free VRAM does not end the test: a larger context can need less. Ollama (0.31.2 on) keeps a
                # vision model's image encoder in VRAM while there is room and moves it to system RAM when there is
                # not (qwen3.5:9b: 7.4 GiB in use at 32k, 7.0 at 64k; measured 2026-10-01). The first load that
                # fails ends it.
            ok = [r for r in results if r["fits"]]
            if not ok:
                await self.gateway.unload(model)
                fit = {**base, "fits": False, "steps": results,
                       "reason": "The model does not load fully into VRAM even at the smallest context."}
                self.registry.set_fit(model, fit)
                return fit
            roomy = [r for r in ok if r.get("vram_free") is None or r["vram_free"] >= VRAM_RESERVE]
            capped = [r for r in (roomy or ok) if r["ctx"] <= max_ctx_setting] or [min(ok, key=lambda r: r["ctx"])]
            chosen = max(capped, key=lambda r: r["ctx"])["ctx"]
            fit = {**base, "fits": True, "steps": results, "max_ctx": max(r["ctx"] for r in ok), "num_ctx": chosen}
            if m["role"] == "chat":
                self._progress(job_id, {"phase": "speed", "ctx": chosen, "steps": results, "of": len(steps)})
                think = False if "thinking" in (info.get("capabilities") or []) else None
                res = None
                async for chunk in self.gateway.chat(
                        model=model, messages=[{"role": "user", "content": SPEED_PROMPT}], kind="fit",
                        num_ctx=chosen, options={"num_predict": 128, "temperature": 0}, think=think,
                        held=ticket, keep_alive="5m", label="speed test"):
                    if chunk.get("done"):
                        res = chunk
                if res and (res.get("eval_count") or 0) >= 16 and res.get("eval_duration"):  # a few tokens time nothing
                    fit["tokens_per_s"] = round(res["eval_count"] / (res["eval_duration"] / 1e9), 1)
                if "tools" in (info.get("capabilities") or []):
                    self._progress(job_id, {"phase": "tools", "ctx": chosen, "steps": results, "of": len(steps)})
                    fit["tools"] = await self._tool_check(model, chosen, ticket, think)
            await self.gateway.unload(model)
        self.registry.set_fit(model, fit)
        return fit

    async def _tool_check(self, model: str, num_ctx: int, ticket, think) -> dict:
        """TOOL_CHECKS on the loaded model: {passed, of, checks: {name: bool}, errors: {name: Ollama's error}, ms}."""
        checks, errors, t0 = {}, {}, mono_ms()
        for key, tools, prompt, ok in TOOL_CHECKS:
            r = {"calls": [], "content": ""}
            try:
                async for chunk in self.gateway.chat(
                        model=model, messages=[{"role": "system", "content": TOOL_CHECK_SYSTEM}, {"role": "user", "content": prompt}],
                        tools=tools, kind="fit", num_ctx=num_ctx, think=think, options={"temperature": 0, "num_predict": 4096},
                        held=ticket, keep_alive="5m", label="tool check"):
                    msg = chunk.get("message") or {}
                    r["content"] += msg.get("content") or ""
                    for c in msg.get("tool_calls") or []:
                        fn = c.get("function") or {}
                        args = fn.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = {}
                        r["calls"].append({"name": fn.get("name"), "args": args if isinstance(args, dict) else {}})
                checks[key] = bool(ok(r))
            except OllamaError as exc:  # e.g. Ollama could not read the tool call
                checks[key] = False
                errors[key] = clip(str(exc), 200)
        out = {"passed": sum(checks.values()), "of": len(checks), "checks": checks, "ms": round(mono_ms() - t0)}
        if errors:
            out["errors"] = errors
        return out

    async def _fit_image(self, job_id: str, m: dict) -> dict:
        """An image model: start its server (every part in VRAM unless staged), check it, and time one image at
        the default size (1024x1024), which is also the size whose memory needs matter."""
        base = {"tested_ms": now_ms(), "weights": m["info"].get("size"), "staged": m["source"].get("staged", False)}
        mem = self.gpu.memory()
        if mem and not base["staged"] and (m["info"].get("size") or 0) > mem["total"] - VRAM_RESERVE:
            fit = {**base, "fits": False, "reason": "Its files are larger than the GPU's memory; it could only run staged."}
            self.registry.set_fit(m["name"], fit)
            return fit
        self._progress(job_id, {"phase": "loading"})
        ticket = Ticket(kind="fit", model=m["name"], label=f"GPU fit test: {m['name']}")
        d = m["source"].get("defaults") or {}
        try:
            async with self.gateway.exclusive(ticket):
                res = await self.gateway.image(m["name"], {"prompt": "A white sheep on a green hill, soft morning light",
                                                           "width": 1024, "height": 1024, "steps": d.get("steps"),
                                                           "cfg": d.get("cfg"), "seed": 1},
                                               held=ticket, kind="fit", keep_alive=0,
                                               on_progress=lambda p: self._progress(job_id, {"phase": "image", **p}))
        except (ResidencyError, OllamaError) as exc:
            fit = {**base, "fits": False, "reason": str(exc)[:400]}
            self.registry.set_fit(m["name"], fit)
            return fit
        fit = {**base, "fits": True, "num_ctx": 0, "max_ctx": 0, "seconds_1024": res["seconds"],
               "server": {k: res["load"].get(k) for k in ("vram_bytes", "rss_anon_bytes", "ram", "load_ms")}}
        self.registry.set_fit(m["name"], fit)
        return fit

    # installs -----------------------------------------------------------------------------------
    def start_pull(self, model: str, account_id: str | None, test_after: bool = True) -> str:
        existing = self.running("pull", model)
        if existing:
            return existing
        return self._start("pull", account_id, {"model": model},
                           lambda job_id: self._pull(job_id, model, account_id, test_after))

    async def _pull(self, job_id: str, model: str, account_id, test_after: bool) -> dict:
        layers: dict[str, tuple[int, int]] = {}
        status, last_pub, t0 = "starting", 0.0, mono_ms()
        try:
            async for p in self.ollama.pull(model):
                status = p.get("status", status)
                if p.get("digest") and p.get("total"):
                    layers[p["digest"]] = (int(p.get("completed") or 0), int(p["total"]))
                now = mono_ms()
                if now - last_pub > 300 or status == "success":
                    done = sum(c for c, _ in layers.values())
                    total = sum(t for _, t in layers.values())
                    elapsed = max((now - t0) / 1000, 0.001)
                    self._progress(job_id, {"status": status, "completed": done, "total": total,
                                            "rate": round(done / elapsed), "layers": len(layers)})
                    last_pub = now
        except OllamaError as exc:
            if "newer version of Ollama" in str(exc):
                raise OllamaError(await self._needs_newer_ollama(model)) from None
            plain = pull_error(model, str(exc))
            if plain:
                raise OllamaError(f"{plain} (Ollama said: {clip(str(exc), 200)})") from None
            raise
        await self.registry.sync()
        result = {"model": model, "installed": self.registry.get(model) is not None}
        if test_after and result["installed"]:
            result["fit_job"] = self.start_fit(model, account_id)
        return result

    async def _needs_newer_ollama(self, model: str) -> str:
        try:
            have = await self.ollama.version()
        except (OllamaError, OSError):
            have = None
        requires = None
        name, _, tag = model.partition(":")
        if "/" not in name:
            requires = (await library.fetch_registry_info(name, tag or "latest"))["requires"]
        need = f"Ollama {requires} or newer" if requires else "a newer Ollama"
        return (f"{model} needs {need}, and this computer has {f'Ollama {have}' if have else 'an older one'}. "
                "Update Ollama, then install the model again.")

    async def remove(self, model: str) -> None:
        await self.gateway.unload(model)
        await self.ollama.delete(model)
        await self.registry.sync()

    # check for newer models ---------------------------------------------------------------------
    def start_scout(self, account_id: str | None, other_families: bool = False, model: str | None = None,
                    query: str | None = None, category: str | None = None) -> str:
        """Newer versions of every installed model or of `model`, or a search by `query` or `category`.
        One runs at a time: the same request returns the running job, a different one replaces it."""
        params = {"other_families": other_families, "model": model, "query": query, "category": category}
        existing = self.running("scout")
        if existing:
            if (self.maindb.job_get(existing) or {}).get("params") == params:
                return existing
            self.cancel(existing)
        return self._start("scout", account_id, params, lambda job_id: self._scout(job_id, account_id, params))

    async def _scout(self, job_id: str, account_id, params: dict) -> dict:
        from .scout import scout, search
        if params["query"] or params["category"]:
            return await search(self, job_id, account_id, params["query"] or "", params["category"])
        return await scout(self, job_id, account_id, params["other_families"], params["model"])
