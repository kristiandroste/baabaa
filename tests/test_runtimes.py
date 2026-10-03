"""Models run by other programs than Ollama: GGUF metadata, the llama.cpp server engine and its GPU checks.

A stand-in server (fixtures/fake_llama_server.py) plays llama-server; nothing here uses the GPU.
"""

import asyncio
import os
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from baabaa import gguf  # noqa: E402
from baabaa.gateway import Gateway, ResidencyError  # noqa: E402
from baabaa import llamacpp  # noqa: E402
from baabaa.llamacpp import (LlamaCppError, LlamaCppManager, _StreamState, command, keep_alive_seconds,  # noqa: E402
                             media_part, parse_log, to_openai)
from baabaa.maindb import MainDB  # noqa: E402
from baabaa.models import ModelRegistry  # noqa: E402
from baabaa.ollama import Ollama  # noqa: E402
from baabaa.paths import Paths  # noqa: E402
from baabaa.stats import Stats  # noqa: E402

FAKE_SERVER = ROOT / "tests" / "fixtures" / "fake_llama_server.py"


def write_gguf(path: str, meta: dict, tensors: list[tuple[str, int]]) -> None:
    def s(x: str) -> bytes:
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    kv = b""
    for k, v in meta.items():
        kv += s(k)
        if isinstance(v, str):
            kv += struct.pack("<I", 8) + s(v)
        elif isinstance(v, bool):
            kv += struct.pack("<I", 7) + struct.pack("<?", v)
        elif isinstance(v, int):
            kv += struct.pack("<I", 4) + struct.pack("<I", v)
        elif isinstance(v, list):
            kv += struct.pack("<I", 9) + struct.pack("<I", 8) + struct.pack("<Q", len(v)) + b"".join(s(x) for x in v)
    ti, off = b"", 0
    for name, nb in tensors:
        ti += s(name) + struct.pack("<I", 1) + struct.pack("<Q", nb) + struct.pack("<I", 0) + struct.pack("<Q", off)
        off += -(-nb // 32) * 32
    head = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(meta)) + kv + ti
    head += b"\0" * (-len(head) % 32)
    with open(path, "wb") as f:
        f.write(head + b"\0" * off)


class FakeGPU:
    available = False

    def memory(self):
        return None

    def energy_mj(self):
        return None

    def process_used(self, pid):
        return None

    def sample(self):
        return {}


class GGUFTest(unittest.TestCase):
    def test_describe(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.gguf")
            write_gguf(p, {"general.architecture": "qwen35", "general.size_label": "27B", "qwen35.context_length": 262144,
                           "qwen35.block_count": 4, "qwen35.attention.head_count": 8, "qwen35.attention.head_count_kv": 2,
                           "qwen35.attention.key_length": 128, "qwen35.full_attention_interval": 4,
                           "tokenizer.ggml.tokens": [f"t{i}" for i in range(100)],
                           "tokenizer.chat_template": "{% if tools %}…{% endif %}<think>"},
                       [("token_embd.weight", 1000), ("blk.0.attn.weight", 5000), ("blk.1.attn.weight", 5000),
                        ("output.weight", 1000)])
            g = gguf.read(p)
            self.assertEqual(g["metadata"]["tokenizer.ggml.tokens"], {"array_of": 8, "count": 100})
            self.assertEqual(g["tensors"]["layers"], 2)
            self.assertEqual(g["tensors"]["token_embd_bytes"], 1024)  # padded to the 32-byte alignment
            facts = gguf.describe(p)
            self.assertEqual(facts["context_length"], 262144)
            self.assertEqual(facts["head_dim"], 128)
            self.assertIn("tools", facts["capabilities"])
            self.assertIn("thinking", facts["capabilities"])
            with open(p, "r+b") as f:
                f.write(b"NOPE")
            with self.assertRaises(gguf.GGUFError):
                gguf.read(p)


class ConversionTest(unittest.TestCase):
    def test_request(self):
        body = to_openai({
            "model": "x", "think": False, "format": {"type": "object"},
            "options": {"num_ctx": 8192, "num_predict": 64, "temperature": 0.2},
            "tools": [{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
            "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi", "images": ["QUJD"]},
                         {"role": "assistant", "content": "", "tool_calls": [
                             {"function": {"name": "web_search", "arguments": {"query": "sheep"}}}]},
                         {"role": "tool", "tool_name": "web_search", "content": "results"}]})
        self.assertEqual(body["max_tokens"], 64)
        self.assertNotIn("num_ctx", body)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["response_format"]["type"], "json_schema")
        user = body["messages"][1]
        self.assertEqual(user["content"][1]["image_url"]["url"], "data:image/png;base64,QUJD")
        call = body["messages"][2]["tool_calls"][0]
        self.assertEqual(call["function"]["arguments"], '{"query": "sheep"}')
        self.assertEqual(body["messages"][3]["tool_call_id"], call["id"])

    def test_stream(self):
        st = _StreamState()
        out = []
        for item in ({"choices": [{"delta": {"reasoning_content": "hm"}}]},
                     {"choices": [{"delta": {"content": "Hel"}}]},
                     {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "todo", "arguments": '{"a":'}}]}}]},
                     {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": " 1}"}}]}}]},
                     {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                      "timings": {"prompt_n": 10, "cache_n": 5, "prompt_ms": 20, "predicted_n": 4, "predicted_ms": 40}}):
            out += list(st.feed(item))
        final = st.finish()
        self.assertEqual(out[0]["message"]["thinking"], "hm")
        self.assertEqual(out[1]["message"]["content"], "Hel")
        self.assertEqual(final["message"]["tool_calls"], [{"function": {"name": "todo", "arguments": {"a": 1}}}])
        self.assertEqual((final["prompt_eval_count"], final["eval_count"]), (15, 4))
        self.assertEqual(final["eval_duration"], 40_000_000)

    def test_helpers(self):
        self.assertEqual(keep_alive_seconds("5m"), 300)
        self.assertIsNone(keep_alive_seconds(-1))
        self.assertEqual(keep_alive_seconds(0), 0)
        log = parse_log("load_tensors: offloaded 64/65 layers to GPU\nload_tensors:   CUDA0 model buffer size =  12.50 MiB")
        self.assertEqual(log["offloaded"], [64, 65])
        self.assertEqual(log["buffers_mib"], {"CUDA0": 12.5})

    def test_placement_log(self):
        # lines as llama-server -lv 4 writes them (a hybrid model with an image encoder)
        log = parse_log("""0.00.926 I load_tensors: offloaded 34/34 layers to GPU
0.00.926 I load_tensors:          CPU model buffer size =   545.62 MiB
0.00.926 I load_tensors:        CUDA0 model buffer size =  4861.36 MiB
0.11.813 I llama_kv_cache:      CUDA0 KV buffer size =   512.00 MiB
0.11.813 I llama_kv_cache:      CUDA0 KV buffer size =    12.00 MiB
0.11.814 I llama_memory_recurrent:      CUDA0 RS buffer size =    50.25 MiB
0.46.249 I clip_ctx: CLIP using CUDA0 backend
0.48.666 I srv    load_model: prompt cache is disabled - use `--cache-ram N` to enable it
0.48.666 I srv    load_model: context checkpoints disabled""")
        self.assertEqual(log["buffers_mib"], {"CPU": 545.62, "CUDA0": 4861.36})
        self.assertEqual((log["kv_mib"], log["rs_mib"], log["encoder"]), ({"CUDA0": 524.0}, {"CUDA0": 50.25}, ["CUDA0"]))
        self.assertEqual((log["ram_cache_mib"], log["checkpoints"]), (0, 0))
        log = parse_log("clip_ctx: CLIP using CPU backend\nprompt cache is enabled, size limit: 8192 MiB\n"
                        "context checkpoints enabled, max = 32, min spacing = 256")
        self.assertEqual((log["encoder"], log["ram_cache_mib"], log["checkpoints"]), (["CPU"], 8192, 32))

    def test_command(self):
        src = {"server": "/opt/llama/llama-server", "model": "/m.gguf", "mmproj": "/m.gguf", "backend": "/opt/cuda/libggml-cuda.so"}
        full = ("-dev,  --device <d>\n-cram, --cache-ram N\n-ctxcp, --ctx-checkpoints N\n-cms, --checkpoint-min-step N\n"
                "-lv, --log-verbosity N\n--no-webui\n-kvu,  --kv-unified, -no-kvu, --no-kv-unified\n"
                "--cache-idle-slots, --no-cache-idle-slots")
        argv, env = command(src, 8192, 1234, "k", help=full)
        line = " ".join(argv)
        for part in ("-ngl 999", "-dev CUDA0", "--cache-ram 1024", "--ctx-checkpoints 4", "--checkpoint-min-step 256", "-lv 4",
                     "--no-mmap", "--mmproj /m.gguf", "-np 2 -ngl 999 --no-mmap --jinja --kv-unified --no-cache-idle-slots"):
            self.assertIn(part, line)
        self.assertEqual((env["GGML_BACKEND_PATH"], env["LLAMA_ARG_FIT"], env["LLAMA_API_KEY"]), (src["backend"], "off", "k"))
        # an older build without the RAM caches or a shared context gets no flags it does not know, and one slot
        argv, _ = command(src, 8192, 1234, help="-lv, --log-verbosity N")
        self.assertNotIn("--ctx-checkpoints", argv)
        self.assertNotIn("--cache-ram", argv)
        self.assertEqual(argv[argv.index("-np") + 1], "1")
        for bad in (["-nkvo"], ["--cache-ram", "4096"], ["--ctx-checkpoints=8"], ["-ot", "blk=CPU"], ["--no-mmproj-offload"],
                    ["--n-cpu-moe", "4"], ["-ngl", "20"]):
            with self.assertRaises(LlamaCppError):
                command({**src, "args": bad}, 8192, 1234)
        command({**src, "args": ["-fa", "on", "-b", "512"]}, 8192, 1234)  # allowed

    def test_image_server_command(self):
        from baabaa import sdcpp
        src = {"server": "/opt/sd/sd-server", "diffusion_model": "/d.gguf", "llm": "/t.gguf", "vae": "/v.safetensors"}
        full = "--backend <s>\n--params-backend <s>\n--auto-fit on|off\n--eager-load\n--conditioning-cache-size <int>"
        line = " ".join(sdcpp.command(src, 1234, help=full)[0])
        for part in ("--backend CUDA0", "--params-backend CUDA0", "--auto-fit off", "--eager-load", "--conditioning-cache-size 0"):
            self.assertIn(part, line)
        self.assertNotIn("--params-backend", " ".join(sdcpp.command(src, 1234, help="--vae-tiling")[0]))  # older build
        staged = " ".join(sdcpp.command({**src, "staged": True}, 1234, help=full)[0])
        self.assertIn("--backend CUDA0 --params-backend disk", staged)  # each part read from its file into VRAM
        self.assertNotIn("--offload-to-cpu", staged)
        for bad in (["--params-backend", "te=cpu"], ["--offload-to-cpu"], ["--auto-fit=on"], ["--vae-on-cpu"]):
            with self.assertRaises(sdcpp.SdError):
                sdcpp.command({**src, "args": bad}, 1234)
        log = sdcpp.parse_log("[INFO] total params memory size = 6221.12MB (VRAM 3685.21MB, RAM 2535.91MB): text_encoders ...")
        self.assertEqual((log["params_vram_mb"], log["params_ram_mb"]), (3685.21, 2535.91))

    def test_ollama_caps(self):
        from baabaa.ollama import ram_caps_missing
        self.assertEqual(ram_caps_missing({"LLAMA_ARG_FIT": "off"}), ["LLAMA_ARG_CACHE_RAM=1024", "LLAMA_ARG_CTX_CHECKPOINTS=8"])
        self.assertEqual(ram_caps_missing({"LLAMA_ARG_CACHE_RAM": "-1", "LLAMA_ARG_CTX_CHECKPOINTS": "4"}), ["LLAMA_ARG_CACHE_RAM=1024"])
        self.assertEqual(ram_caps_missing({"LLAMA_ARG_CACHE_RAM": "0", "LLAMA_ARG_CTX_CHECKPOINTS": "8"}), [])
        self.assertIsNone(ram_caps_missing(None))

    def test_cpu_seconds(self):
        from baabaa import cpuwatch
        before = cpuwatch.cpu_seconds([os.getpid()])
        end = __import__("time").process_time() + 0.2
        while __import__("time").process_time() < end:
            pass
        self.assertGreaterEqual(cpuwatch.cpu_seconds([os.getpid()]) - before, 0.15)
        self.assertIsNone(cpuwatch.cpu_seconds([2**31 - 1]))

    def test_media_part(self):
        import base64
        wav = base64.b64encode(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20).decode()
        self.assertEqual(media_part(wav), {"type": "input_audio", "input_audio": {"data": wav, "format": "wav"}})
        jpg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 20).decode()
        self.assertTrue(media_part(jpg)["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20).decode()
        self.assertTrue(media_part(png)["image_url"]["url"].startswith("data:image/png;base64,"))


@unittest.skipUnless(sys.platform == "linux", "models outside Ollama run on Linux only")
class EngineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = Paths(root / "data")
        self.maindb = MainDB(self.paths.main_db)
        self.stats = Stats(self.paths.stats_db)
        self.ollama = Ollama("http://127.0.0.1:9")  # nothing listens: Ollama "holds nothing"
        self.registry = ModelRegistry(self.maindb, self.ollama)
        self.manager = LlamaCppManager(self.paths, FakeGPU())
        self.gateway = Gateway(self.ollama, self.stats, FakeGPU(), self.registry, llamacpp=self.manager)
        mode = os.stat(FAKE_SERVER).st_mode
        os.chmod(FAKE_SERVER, mode | stat.S_IXUSR)
        self.models = {}
        for name in ("good", "partial", "crash", "tools", "kvcpu", "encodercpu", "cpuburn"):
            p = str(root / f"{name}.gguf")
            write_gguf(p, {"general.architecture": "llama", "llama.context_length": 32768, "llama.block_count": 2,
                           "tokenizer.chat_template": "{{ tools }}"}, [("token_embd.weight", 64), ("blk.0.w", 256)])
            self.models[name] = p

    async def asyncTearDown(self):
        await self.manager.stop()
        self.tmp.cleanup()

    def add(self, name, **extra):
        return self.registry.add_llamacpp(f"m-{name}", {"server": str(FAKE_SERVER), "model": self.models[name], **extra})

    async def chat(self, name, **kw):
        chunks = []
        async for c in self.gateway.chat(model=f"m-{name}", num_ctx=4096, messages=[{"role": "user", "content": "baa"}],
                                         account_id="a", **kw):
            chunks.append(c)
        return chunks

    async def test_register_and_chat(self):
        m = self.add("good")
        self.assertEqual((m["runtime"], m["role"]), ("llamacpp", "chat"))
        self.assertTrue(m["digest"].startswith("file:"))
        with self.assertRaises(ValueError):
            self.registry.add_llamacpp("bad:name", {"server": str(FAKE_SERVER), "model": self.models["good"]})
        with self.assertRaises(ValueError):
            self.registry.add_llamacpp("m-other", {"server": "/nonexistent", "model": self.models["good"]})
        chunks = await self.chat("good", think=True)
        text = "".join(c["message"].get("content", "") for c in chunks)
        self.assertEqual(text, "Echo: baa")
        self.assertEqual(chunks[0]["message"]["thinking"], "Thinking briefly.")
        self.assertEqual(chunks[-1]["eval_count"], 5)
        running = self.manager.running()
        self.assertEqual((running["name"], running["num_ctx"], running["offloaded"]), ("m-good", 4096, [65, 65]))
        # a second request reuses the running server
        pid = running["pid"]
        await self.chat("good", think=False)
        self.assertEqual(self.manager.running()["pid"], pid)
        row = self.stats.con.execute("SELECT outcome, output_tokens, prompt_tokens FROM model_requests ORDER BY ts_ms DESC").fetchone()
        self.assertEqual(tuple(row), ("ok", 5, 15))
        await self.gateway.unload("m-good")
        self.assertIsNone(self.manager.running())

    async def test_tool_call(self):
        self.add("tools")
        chunks = []
        async for c in self.gateway.chat(model="m-tools", num_ctx=4096, messages=[{"role": "user", "content": "find sheep"}],
                                         tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}]):
            chunks.append(c)
        self.assertEqual(chunks[-1]["message"]["tool_calls"], [{"function": {"name": "web_search", "arguments": {"query": "sheep"}}}])

    async def test_refusals(self):
        self.add("partial")
        with self.assertRaises(ResidencyError) as cm:
            await self.chat("partial")
        self.assertIn("40 of 65 layers", str(cm.exception))
        self.assertIsNone(self.manager.running())
        self.add("crash")
        with self.assertRaises(LlamaCppError) as cm:
            await self.chat("crash")
        self.assertIn("failed to allocate", str(cm.exception))
        rows = [r[0] for r in self.stats.con.execute("SELECT outcome FROM model_requests")]
        self.assertEqual(sorted(rows), ["error", "residency"])
        # live context and the encoder must be on the GPU too
        self.add("kvcpu")
        with self.assertRaises(ResidencyError) as cm:
            await self.chat("kvcpu")
        self.assertIn("KV cache is in system memory", str(cm.exception))
        self.add("encodercpu", mmproj=self.models["encodercpu"])
        with self.assertRaises(ResidencyError) as cm:
            await self.chat("encodercpu")
        self.assertIn("encoder runs on the CPU", str(cm.exception))
        self.assertIsNone(self.manager.running())

    async def test_ram_caches_bounded(self):
        # capped caches pass (test_register_and_chat); a server whose caches are at llama.cpp's
        # defaults (8 GiB, 32 checkpoints: the flags left out) is refused
        self.add("good")
        real = llamacpp.command
        llamacpp.command = lambda *a, **kw: real(*a, **{**kw, "help": "-lv, --log-verbosity N"})
        try:
            with self.assertRaises(ResidencyError) as cm:
                await self.chat("good")
        finally:
            llamacpp.command = real
        self.assertIn("RAM caches are not bounded", str(cm.exception))

    async def test_cpu_guard(self):
        # a server that computes on the CPU is stopped, and the request fails with the reason
        self.gateway.cpu_limit, self.gateway.cpu_window, self.gateway.cpu_interval = 0.5, 0.6, 0.2
        self.add("cpuburn")
        with self.assertRaises(ResidencyError) as cm:
            await self.chat("cpuburn")
        self.assertIn("CPU cores busy", str(cm.exception))
        self.assertIsNone(self.manager.running())
        row = self.stats.con.execute("SELECT outcome, cpu_peak FROM model_requests ORDER BY ts_ms DESC").fetchone()
        self.assertEqual(row[0], "cpu")
        self.assertGreater(row[1], 0.5)
        # a server that only waits on the GPU passes, and its CPU use is recorded
        self.add("good")
        await self.chat("good")
        row = self.stats.con.execute("SELECT outcome, cpu_s FROM model_requests ORDER BY ts_ms DESC").fetchone()
        self.assertEqual(row[0], "ok")
        self.assertIsNotNone(row[1])

    async def test_idle_stop(self):
        self.add("good")
        await self.chat("good", keep_alive="0.3s")
        self.assertIsNotNone(self.manager.running())
        await asyncio.sleep(0.8)
        self.assertIsNone(self.manager.running())
        await self.chat("good", keep_alive=0)  # 0 stops the server right after the request
        await asyncio.sleep(0.3)
        self.assertIsNone(self.manager.running())


@unittest.skipUnless(sys.platform == "linux", "on macOS, Ollama runs the encoder on the GPU itself")
class MediaRouteTest(unittest.IsolatedAsyncioTestCase):
    """A turn with an image for an Ollama model: baabaa runs the model's files with Ollama's server
    program itself (encoder on the GPU), instead of sending the image to Ollama."""

    async def asyncSetUp(self):
        sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
        from fake_ollama import CHAT_MODEL, FakeOllama
        self.chat_model = CHAT_MODEL
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # a stand-in Ollama install: its llama-server (the fake) and CUDA library
        (root / "ollama" / "cuda_v12").mkdir(parents=True)
        os.symlink(FAKE_SERVER, root / "ollama" / "llama-server")
        (root / "ollama" / "cuda_v12" / "libggml-cuda.so").write_bytes(b"")
        self.dirs = llamacpp.OLLAMA_DIRS
        llamacpp.OLLAMA_DIRS = (str(root / "ollama"),)
        blob = str(root / "sha256-model")
        write_gguf(blob, {"general.architecture": "qwen35", "tokenizer.chat_template": "{{ tools }}"},
                   [("token_embd.weight", 64), ("blk.0.w", 256), ("v.patch_embd.weight", 128)])
        self.fake = FakeOllama()
        self.fake.files[CHAT_MODEL] = [blob]
        self.fake.vision = True
        self.ollama = Ollama(self.fake.start())
        self.paths = Paths(root / "data")
        self.maindb = MainDB(self.paths.main_db)
        self.stats = Stats(self.paths.stats_db)
        self.registry = ModelRegistry(self.maindb, self.ollama)
        await self.registry.sync()
        self.registry.set_fit(CHAT_MODEL, {"fits": True, "num_ctx": 16384, "max_ctx": 16384, "steps": []})
        self.registry.approve(CHAT_MODEL, True)
        self.manager = LlamaCppManager(self.paths, FakeGPU())
        self.gateway = Gateway(self.ollama, self.stats, FakeGPU(), self.registry, llamacpp=self.manager)

    async def asyncTearDown(self):
        await self.manager.stop()
        llamacpp.OLLAMA_DIRS = self.dirs
        self.fake.stop()
        self.tmp.cleanup()

    async def test_image_turn(self):
        import base64
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).decode()
        self.fake.vision = True
        msgs = [{"role": "user", "content": "What is this?", "images": [png]}]
        res = await self.gateway.complete(model=self.chat_model, messages=msgs)
        self.assertEqual(res["content"], "Saw image/png.")
        running = self.manager.running()
        self.assertEqual((running["name"], running["encoder"]), (self.chat_model, ["CUDA0"]))
        self.assertFalse([r for r in self.fake.requests if r["path"] == "/api/chat" and r.get("messages")])  # nothing to Ollama
        # a text-only request (a title, the next turn) uses the server that is already running
        pid = running["pid"]
        res = await self.gateway.complete(model=self.chat_model, messages=[{"role": "user", "content": "And now?"}])
        self.assertEqual(res["content"], "Echo: And now?")
        self.assertEqual(self.manager.running()["pid"], pid)
        # ... unless it does not fit that server's context: then Ollama runs it, and holds the GPU alone
        self.fake.script([{"content": "Plain text."}])
        res = await self.gateway.complete(model=self.chat_model, messages=[{"role": "user", "content": "baa " * 20000}])
        self.assertEqual(res["content"].strip(), "Plain text.")
        self.assertIsNone(self.manager.running())

    async def test_no_engine(self):
        llamacpp.OLLAMA_DIRS = ("/nonexistent",)
        from baabaa.gateway import ModelNotAllowed
        with self.assertRaises(ModelNotAllowed) as cm:
            await self.gateway.complete(model=self.chat_model, messages=[{"role": "user", "content": "x", "images": ["QUJD"]}])
        self.assertIn("encoder on the CPU", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
