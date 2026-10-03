"""Image models, served by stable-diffusion.cpp's server (`sd-server`), which baabaa starts and stops.

A registered image model names the server program and its files: the diffusion model, the text encoder
(`llm`), the VAE and, for editing, a vision projector. The files are fixed when the server starts, so
baabaa runs one server per model, one at a time, on 127.0.0.1 and a free port, with a clean environment.

The GPU rules:
- a CUDA build, every part computed on the GPU (`--backend CUDA0`), and never the flags that put a stage
  on the CPU (`--clip-on-cpu`, `--vae-on-cpu`, `--control-net-cpu`) or placement flags from the owner;
- a model that fits whole keeps every part in VRAM for the server's life: `--params-backend CUDA0
  --auto-fit off --eager-load` (newer builds' auto-fit would otherwise park the text encoder and VAE
  weights in system RAM and stream them in), checked after start: the load log reports no weights in RAM
  and the process holds at least 90% of the model files' size in VRAM;
- a model too large for that runs staged: each part is read from its file into VRAM for its step
  (`--params-backend disk`) and released after, so system memory stays at normal-program scale. Builds
  without that option can stage only through RAM (`--offload-to-cpu`), which the RAM ceiling refuses
  for large models;
- every server's system memory stays below a runaway ceiling (3 GiB), and the CPU guard (cpuwatch.py)
  stops one that computes on the CPU.
Jobs use the server's own job API (`/sdcpp/v1/img_gen`, polled, cancellable), inside the GPU queue.
"""

import asyncio
import base64
import json
import os
import re
import secrets
import signal
import time

from .llamacpp import LlamaCppError, _free_port, help_text, rss_anon

FILES = ("diffusion_model", "llm", "vae", "llm_vision", "clip_l", "t5xxl", "model")
FLAGS = {"diffusion_model": "--diffusion-model", "llm": "--llm", "vae": "--vae", "llm_vision": "--llm_vision",
         "clip_l": "--clip_l", "t5xxl": "--t5xxl", "model": "--model"}
CPU_FLAGS = {"--clip-on-cpu", "--vae-on-cpu", "--control-net-cpu", "--offload-to-cpu", "--backend", "--params-backend",
             "--auto-fit", "--max-vram", "--rpc-servers", "--eager-load", "--mmap", "--conditioning-cache-size",
             "--disable-prefetch", "--split-mode", "-t", "--threads"}
RAM_CEILING = 3 * 2**30   # a server beyond this has run away (normal programs stay well below)
VRAM_SHARE = 0.90
START_TIMEOUT = 900


class SdError(LlamaCppError):
    pass


def model_bytes(source: dict) -> int:
    return sum(os.path.getsize(source[k]) for k in FILES if source.get(k) and os.path.isfile(source[k]))


def command(source: dict, port: int, help: str | None = None) -> tuple[list[str], dict]:
    """argv and environment; `help`: the server's --help, so that an older build gets only flags it knows."""
    argv = [source["server"], "--listen-ip", "127.0.0.1", "--listen-port", str(port)]
    for key in FILES:
        if source.get(key):
            argv += [FLAGS[key], source[key]]
    extra = [str(a) for a in (source.get("args") or [])]
    bad = [a for a in extra if a.split("=", 1)[0] in CPU_FLAGS]
    if bad:
        raise SdError(f"{', '.join(bad)}: baabaa places the model's parts itself, by the GPU rules")
    has = (lambda flag: True) if not help else (lambda flag: re.search(rf"(^|[\s,]){re.escape(flag)}\b", help) is not None)
    device = source.get("device") or "CUDA0"
    if not has("--params-backend"):  # an older build
        if source.get("staged"):
            argv.append("--offload-to-cpu")  # parts take turns in VRAM from RAM; the RAM ceiling applies
    elif source.get("staged"):
        argv += ["--backend", device, "--params-backend", "disk"]  # each part read into VRAM for its step
    else:
        argv += ["--backend", device, "--params-backend", device]  # every part and its weights on the GPU
        if has("--auto-fit"):
            argv += ["--auto-fit", "off"]
        if has("--eager-load"):
            argv.append("--eager-load")  # all weights into VRAM now, so the checks below see them
    if has("--conditioning-cache-size"):
        argv += ["--conditioning-cache-size", "0"]
    argv += extra
    lib = [os.path.dirname(source["server"])] + [d for d in (source.get("lib_dirs") or []) if d]
    env = {"HOME": source.get("home") or "/tmp", "PATH": "/usr/bin:/bin", "LD_LIBRARY_PATH": ":".join(lib), "LC_ALL": "C.UTF-8",
           "CUDA_CACHE_MAXSIZE": str(2**30)}
    return argv, env


def parse_log(text: str) -> dict:
    """The load log's own account: 'total params memory size = 6221.12MB (VRAM 6221.12MB, RAM 0.00MB)'."""
    m = re.findall(r"total params memory size = ([\d.]+)MB \(VRAM ([\d.]+)MB, RAM ([\d.]+)MB\)", text)
    if not m:
        return {}
    total, vram, ram = (float(x) for x in m[-1])
    return {"params_mb": total, "params_vram_mb": vram, "params_ram_mb": ram}


class SdServer:
    def __init__(self, name: str, source: dict, log_path: str, gpu):
        self.name, self.source, self.log_path, self.gpu = name, source, log_path, gpu
        self.port = _free_port()
        self.proc: asyncio.subprocess.Process | None = None
        self.load: dict = {}
        self.key = None

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
        for key in ("server",) + FILES:
            path = self.source.get(key)
            if path and not os.path.exists(path):
                raise SdError(f"{path} does not exist")
        _, env = command(self.source, self.port)
        helptext = await asyncio.to_thread(help_text, self.source["server"], env)
        argv, env = command(self.source, self.port, help=helptext)
        logf = open(self.log_path, "wb")
        try:
            self.proc = await asyncio.create_subprocess_exec(*argv, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=logf,
                                                             stderr=logf, start_new_session=True)
        finally:
            logf.close()
        t0 = time.monotonic()
        while time.monotonic() - t0 < START_TIMEOUT:
            if not self.alive:
                raise SdError(f"{self.name}: the image server stopped while loading:\n{self._log_tail()}")
            try:
                status, caps = await self._request("GET", "/sdcpp/v1/capabilities", timeout=3)
                if status == 200:
                    self.load["capabilities"] = caps
                    break
            except (OSError, asyncio.TimeoutError, SdError, ValueError):
                pass
            await asyncio.sleep(0.5)
        else:
            await self.stop()
            raise SdError(f"{self.name}: the image server did not become ready in {START_TIMEOUT} s")
        self.load.update({"load_ms": round((time.monotonic() - t0) * 1000), "pid": self.proc.pid, "port": self.port})
        await self.verify()
        return self.load

    async def verify(self) -> dict:
        from .gateway import ResidencyError
        if not self.alive:
            raise ResidencyError(f"{self.name} is not running")
        weights = model_bytes(self.source)
        vram = self.gpu.process_used(self.proc.pid) if self.gpu else None
        rss = rss_anon(self.proc.pid)
        try:
            with open(self.log_path, errors="replace") as f:
                log = parse_log(f.read())
        except OSError:
            log = {}
        self.load.update({"vram_bytes": vram, "rss_anon_bytes": rss, "weights": weights,
                          "staged": bool(self.source.get("staged")), **log})
        problems = []
        if not self.source.get("staged"):
            if log.get("params_ram_mb", 0) > 1:
                problems.append(f"{log['params_ram_mb'] / 1024:.2f} GiB of its weights are in system memory")
            if vram is None:
                problems.append("could not confirm that the model is on the GPU (no per-process GPU reading)")
            elif weights and vram < weights * VRAM_SHARE:
                problems.append(f"the GPU holds {vram / 2**30:.2f} GiB of the model's {weights / 2**30:.2f} GiB")
        if rss is not None and rss > RAM_CEILING:
            problems.append(f"the server holds {rss / 2**30:.2f} GiB of system memory, beyond a normal program's")
        if problems:
            await self.stop()
            raise ResidencyError(f"{self.name} breaks the GPU rules: " + "; ".join(problems))
        return self.load

    async def stop(self) -> None:
        if self.proc is None or self.proc.returncode is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.proc.wait(), 20)
        except asyncio.TimeoutError:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.proc.wait()

    async def _request(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> tuple[int, dict]:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.port, limit=2**26), timeout)
        payload = json.dumps(body).encode() if body is not None else b""
        writer.write((f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload)
        await writer.drain()
        try:
            raw = await asyncio.wait_for(reader.read(), timeout)
        finally:
            writer.close()
        head, _, data = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1]) if lines and len(lines[0].split(" ")) > 1 else 0
        if any(line.lower().startswith("transfer-encoding:") and "chunked" in line.lower() for line in lines[1:]):
            from .llamacpp import _dechunk
            data = _dechunk(data)
        try:
            return status, json.loads(data or b"{}")
        except ValueError:
            return status, {"raw": data[:200].decode(errors="replace")}

    async def generate(self, req: dict, on_progress=None) -> dict:
        """Run one job. `req`: prompt, negative, width, height, steps, seed, cfg, ref_images (bytes). Returns
        {'images': [png bytes], 'seed', 'seconds'}. Cancelling the task cancels the job on the server."""
        body = {"prompt": req["prompt"], "negative_prompt": req.get("negative") or "", "width": int(req.get("width") or 1024),
                "height": int(req.get("height") or 1024), "seed": int(req.get("seed") if req.get("seed") is not None else -1),
                "batch_count": 1, "output_format": "png"}
        sample = {}
        if req.get("steps"):
            sample["sample_steps"] = int(req["steps"])
        if req.get("cfg") is not None:
            sample["guidance"] = {"txt_cfg": float(req["cfg"])}
        if sample:
            body["sample_params"] = sample
        if req.get("ref_images"):
            body["ref_images"] = [base64.b64encode(b).decode() for b in req["ref_images"]]
        t0 = time.monotonic()
        status, job = await self._request("POST", "/sdcpp/v1/img_gen", body)
        if status not in (200, 202) or not job.get("id"):
            raise SdError(f"the image server refused the job: {job.get('error') or job}")
        jid = job["id"]
        try:
            while True:
                await asyncio.sleep(0.5)
                status, job = await self._request("GET", f"/sdcpp/v1/jobs/{jid}")
                state = job.get("status")
                if on_progress:
                    on_progress({"status": state, "seconds": round(time.monotonic() - t0, 1),
                                 "queue_position": job.get("queue_position")})
                if state == "completed":
                    res = job.get("result") or {}
                    images = [base64.b64decode(im["b64_json"]) for im in res.get("images") or [] if im.get("b64_json")]
                    if not images:
                        raise SdError("the image server returned no image")
                    return {"images": images, "seconds": round(time.monotonic() - t0, 2), "seed": body["seed"]}
                if state in ("failed", "cancelled"):
                    err = job.get("error") or {}
                    raise SdError(f"image generation {state}: {err.get('message') or err or ''}".strip())
                if not self.alive:
                    raise SdError(f"the image server stopped:\n{self._log_tail()}")
        except asyncio.CancelledError:
            try:
                await self._request("POST", f"/sdcpp/v1/jobs/{jid}/cancel", {}, timeout=5)
            except (OSError, asyncio.TimeoutError, SdError, ValueError):
                pass
            raise


class SdManager:
    """At most one image server at a time; it stops after `keep_alive` seconds without use."""

    def __init__(self, paths, gpu):
        self.paths, self.gpu = paths, gpu
        self.current: SdServer | None = None
        self._idle = None
        self._lock = asyncio.Lock()

    def running(self) -> dict | None:
        s = self.current
        if s is None or not s.alive:
            return None
        return {"name": s.name, "pid": s.proc.pid, **{k: v for k, v in s.load.items() if k != "capabilities"}}

    async def ensure(self, name: str, source: dict) -> SdServer:
        key = (name, json.dumps(source, sort_keys=True))
        async with self._lock:
            s = self.current
            if s and s.alive and s.key == key:
                return s
            if s is not None:
                await s.stop()
                self.current = None
            log_dir = self.paths.root / "runtime"
            log_dir.mkdir(exist_ok=True, mode=0o700)
            s = SdServer(name, {**source, "home": str(log_dir)}, str(log_dir / "sd-server.log"), self.gpu)
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

    async def stop(self) -> None:
        async with self._lock:
            if self.current is not None:
                await self.current.stop()
                self.current = None


def new_seed() -> int:
    return secrets.randbelow(2**31 - 1)
