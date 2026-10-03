#!/usr/bin/env python3
"""A stand-in for llama.cpp's llama-server in tests: same flags, log lines and chat API; no model, no GPU.

Behaviour depends on the model file's name: 'partial' logs that only some layers are on the GPU,
'crash' exits while loading, 'tools' answers with a tool call, 'kvcpu' puts the KV cache in system
memory, 'encodercpu' runs the image/audio encoder on the CPU, 'cpuburn' keeps a CPU core busy for a few
seconds before it answers. Like llama-server, it reports its RAM prompt cache (8 GiB unless `--cache-ram`
says otherwise) and context checkpoints (32 unless `--ctx-checkpoints`).
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


if "--help" in sys.argv:
    print("-dev,  --device <dev1,dev2,..>\n-cram, --cache-ram N\n-ctxcp, --ctx-checkpoints, --swa-checkpoints N\n"
          "-cms,  --checkpoint-min-step N\n-kvu,  --kv-unified, -no-kvu, --no-kv-unified\n"
          "--cache-idle-slots, --no-cache-idle-slots\n"
          "-lv,  --verbosity, --log-verbosity N\n--webui, --no-webui\n--mmproj-offload, --no-mmproj-offload")
    sys.exit(0)

MODEL = arg("-m", "")
PORT = int(arg("--port", "0"))
CTX = int(arg("-c", "4096"))
log = sys.stderr

print(f"load_model: loading model '{MODEL}'", file=log, flush=True)
if arg("-dev", "CUDA0") != "CUDA0":
    print("error: invalid device", file=log, flush=True)
    sys.exit(1)
if "crash" in MODEL:
    print("error: failed to allocate CUDA0 buffer", file=log, flush=True)
    sys.exit(1)
n = 40 if "partial" in MODEL else 65
print(f"load_tensors: offloaded {n}/65 layers to GPU", file=log, flush=True)
print("load_tensors:        CUDA0 model buffer size =  5395.44 MiB", file=log, flush=True)
print("load_tensors:    CUDA_Host model buffer size =     0.00 MiB", file=log, flush=True)
kv = "CPU" if "kvcpu" in MODEL else "CUDA0"
print(f"llama_kv_cache:      {kv} KV buffer size =   {CTX * 64 / 1024:.2f} MiB", file=log, flush=True)
if "--mmproj" in sys.argv:
    cpu = "encodercpu" in MODEL or "--no-mmproj-offload" in sys.argv
    print(f"clip_ctx: CLIP using {'CPU' if cpu else 'CUDA0'} backend", file=log, flush=True)
cache, points = arg("--cache-ram", "8192"), arg("--ctx-checkpoints", "32")
print("srv    load_model: " + ("prompt cache is disabled" if cache == "0" else f"prompt cache is enabled, size limit: {cache} MiB"),
      file=log, flush=True)
print("srv    load_model: " + ("context checkpoints disabled" if points == "0" else f"context checkpoints enabled, max = {points}, min spacing = 256"),
      file=log, flush=True)
READY_AT = time.time() + 0.3


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            if time.time() < READY_AT:
                return self._json(503, {"error": {"message": "Loading model"}})
            return self._json(200, {"status": "ok"})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        key = os.environ.get("LLAMA_API_KEY")
        if key and self.headers.get("Authorization") != f"Bearer {key}":
            return self._json(401, {"error": {"message": "Invalid API Key"}})
        if "--jinja" not in sys.argv:
            return self._json(500, {"error": {"message": "tools need --jinja"}})
        if self.path != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "not found"}})
        last = body["messages"][-1]
        text = last["content"] if isinstance(last["content"], str) else json.dumps(last["content"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def send(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        if "cpuburn" in MODEL:  # model work on the CPU: one core flat out for a few seconds
            end = time.time() + 4
            while time.time() < end:
                pass
        think = (body.get("chat_template_kwargs") or {}).get("enable_thinking", True)
        if think:
            send({"choices": [{"index": 0, "delta": {"reasoning_content": "Thinking briefly."}}]})
        if "tools" in MODEL and body.get("tools") and last["role"] == "user":
            send({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                  "function": {"name": "web_search", "arguments": '{"query": '}}]}}]})
            send({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"sheep"}'}}]}}]})
            finish = "tool_calls"
        elif isinstance(last["content"], list) and any(p.get("type") != "text" for p in last["content"]):
            kinds = [p["input_audio"]["format"] if p["type"] == "input_audio" else p["image_url"]["url"].split(";")[0][5:]
                     for p in last["content"] if p.get("type") != "text"]
            reply = "Count the lambs in the top field." if kinds == ["wav"] else f"Saw {', '.join(kinds)}."
            send({"choices": [{"index": 0, "delta": {"content": reply}}]})
            finish = "stop"
        else:
            for word in ["Echo:", " ", text[:200]]:
                send({"choices": [{"index": 0, "delta": {"content": word}}]})
            finish = "stop"
        send({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
              "timings": {"prompt_n": 12, "cache_n": 3, "prompt_ms": 40.0, "predicted_n": 5, "predicted_ms": 50.0}})
        self.wfile.write(b"data: [DONE]\n\n")


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
