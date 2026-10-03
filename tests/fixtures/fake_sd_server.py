#!/usr/bin/env python3
"""A stand-in for stable-diffusion.cpp's sd-server in tests: the job API with a tiny PNG; no model, no GPU."""

import base64
import json
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def png(w=8, h=8, rgb=(250, 250, 245)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


if "--help" in sys.argv:
    print("  --backend <string>\n  --params-backend <string>\n  --auto-fit  on|off\n  --eager-load\n"
          "  --conditioning-cache-size <int>\n  --offload-to-cpu")
    sys.exit(0)
PORT = int(arg("--listen-port", "0"))
if "--clip-on-cpu" in sys.argv:
    sys.exit("refusing a CPU flag")
print(f"loading diffusion model {arg('--diffusion-model')}", file=sys.stderr, flush=True)
# like sd.cpp's auto-fit: without an explicit --params-backend the text encoder's weights go to RAM
ram = 0.0 if arg("--params-backend") == "CUDA0" else 2375.91
print(f"total params memory size = {3685.21 + 2375.91:.2f}MB (VRAM {3685.21 + 2375.91 - ram:.2f}MB, RAM {ram:.2f}MB)",
      file=sys.stderr, flush=True)
JOBS, LOCK = {}, threading.Lock()


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
        if self.path == "/sdcpp/v1/capabilities":
            return self._json(200, {"model": "fake", "modes": ["img_gen"], "offload": "--offload-to-cpu" in sys.argv})
        if self.path.startswith("/sdcpp/v1/jobs/"):
            jid = self.path.rsplit("/", 1)[1]
            with LOCK:
                job = JOBS.get(jid)
            if not job:
                return self._json(404, {"error": {"code": "not_found", "message": "no such job"}})
            if job["status"] == "queued" and time.time() - job["t"] > 0.2:
                job["status"] = "generating"
            if job["status"] == "generating" and time.time() - job["t"] > 0.6:
                job["status"] = "completed"
                job["result"] = {"output_format": "png", "images": [{"index": 0, "b64_json": base64.b64encode(png()).decode()}]}
            return self._json(200, {k: v for k, v in job.items() if k != "t"})
        self._json(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/sdcpp/v1/img_gen":
            if not body.get("prompt"):
                return self._json(400, {"error": {"message": "prompt is required"}})
            jid = f"job{len(JOBS) + 1}"
            with LOCK:
                JOBS[jid] = {"id": jid, "status": "queued", "t": time.time(), "prompt": body["prompt"],
                             "refs": len(body.get("ref_images") or [])}
            return self._json(202, {"id": jid, "kind": "img_gen", "status": "queued", "poll_url": f"/sdcpp/v1/jobs/{jid}"})
        if self.path.endswith("/cancel"):
            jid = self.path.split("/")[-2]
            with LOCK:
                if jid in JOBS:
                    JOBS[jid]["status"] = "cancelled"
            return self._json(200, {"id": jid, "status": "cancelled"})
        self._json(404, {})


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
