#!/usr/bin/env python3
"""Builds baabaa's website into dist/site: a landing page, the documents, and a preview of the real interface
that plays recorded replies in the browser. Nothing is published from here.

  python3 tools/website.py build [--out DIR] [--domain NAME]
  python3 tools/website.py serve [--port N]       build, then serve it on this computer only, to look at

The preview is baabaa's own web files, unchanged, plus website/preview.js: a stand-in for the server that
answers the app's requests from a snapshot and plays recorded replies back. The snapshot and the recordings
are made here, by running the real server (in this process, on 127.0.0.1) against the test suite's stand-in
for Ollama, with the conversations in website/script.py.

Nothing of this machine goes into the result: the GPU is a stand-in too, folders are renamed, and the build
fails if this machine's home folder, host name, user name or network addresses appear in what it made, or a
private word does (the list tools/release.py uses, when there is one).
"""

import argparse
import asyncio
import getpass
import http.client
import json
import os
import re
import runpy
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "website"
WEB = ROOT / "baabaa" / "web"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
sys.path.insert(0, str(ROOT / "tools"))

GUEST = "/home/guest"            # where the preview says things are
MODEL = "scripted:4b"            # what the preview calls its stand-in model
RATE = 38
DOMAIN = "baabaa.kdro.ai"   # where the site lives: the sharing tags need absolute addresses                        # tokens a second the preview plays recorded text at (website/preview.js)
DOCUMENTS = ["README.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md", "CLA.md", "LICENSE"]
# what the app reads when it starts or opens a page, taken from the real server as it answers
SNAPSHOT = ["/api/me", "/api/profiles", "/api/health", "/api/status", "/api/models", "/api/jobs", "/api/shares", "/api/projects",
            "/api/schedules", "/api/memory", "/api/artifacts", "/api/images", "/api/rules", "/api/extend", "/api/connectors",
            "/api/settings", "/api/update", "/api/network", "/api/stats/summary", "/api/stats/gpu", "/api/accounts", "/api/me/keys"]
APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
           "media-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; frame-src 'self'; "
           "worker-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'self'")
# an artifact's page is written by the model: it gets no network at all, as on the real server (server/api.py)
ARTIFACT_CSP = ("default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' data: blob:; style-src 'unsafe-inline' data:; "
                "img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; form-action 'none'")


class Stage:
    """The real server on 127.0.0.1, with stand-ins for Ollama and the GPU, and a browser's view of it."""

    def __init__(self, script: dict):
        self.script = script
        self.events: list[tuple[str, dict]] = []
        self.cookie = self.csrf = None

    def __enter__(self):
        root = Path(tempfile.mkdtemp(prefix="baabaa-site-")).resolve()
        self.root, self.home, self.projects = root, root / "data", root / "projects"
        self.work = self.projects / self.script["PROJECT_NAME"]
        self.work.mkdir(parents=True)
        for name, text in self.script["PROJECT"].items():
            (self.work / name).write_text(text)
        self.saved = {k: os.environ.get(k) for k in ("BAABAA_HOME", "BAABAA_OLLAMA_URL", "BAABAA_DISABLE_UPDATES")}
        from fake_ollama import FakeOllama
        self.ollama = FakeOllama()
        self.ollama_url = self.ollama.start()
        os.environ.update(BAABAA_HOME=str(self.home), BAABAA_OLLAMA_URL=self.ollama_url, BAABAA_DISABLE_UPDATES="1")

        import baabaa.app
        from baabaa.gpu import GPU
        from baabaa.server.api import Web
        from baabaa.server.http import Server
        from baabaa.server.lan import LanGuard

        class SampleGPU(GPU):
            """The preview must not carry the graphics card of the machine that built it."""

            def __init__(self, index: int = 0):
                self._lib = self._handle = self.error = self._apple_total = None
                self._lock = threading.Lock()
                self.name, self.kind = "Sample GPU, 16 GB", "nvidia"

            available = property(lambda self: True)
            readings = property(lambda self: False)

            def memory(self):
                return {"total": 16 * 2**30, "used": 4 * 2**30, "free": 12 * 2**30}

        started = threading.Event()
        self.loop = asyncio.new_event_loop()

        def run():
            asyncio.set_event_loop(self.loop)
            with mock.patch.object(baabaa.app, "GPU", SampleGPU):
                self.app = baabaa.app.App(ollama_url=self.ollama_url)
            web = Web(self.app, LanGuard(bind=["127.0.0.1"], networks=[], local_only=True), tls=False, port=8443,
                      setup_token="preview", log=lambda *a, **k: None, plain_http=True)
            server = self.loop.run_until_complete(Server(web).listen_tcp("127.0.0.1", 0))
            self.port = server.sockets[0].getsockname()[1]
            started.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        if not started.wait(30):
            raise SystemExit("The server did not start.")
        return self

    def __exit__(self, *exc):
        async def close():
            await self.app.shutdown()
        try:
            asyncio.run_coroutine_threadsafe(close(), self.loop).result(20)
        except Exception:  # noqa: BLE001 - the build's result is already written or already failed
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.ollama.stop()
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(self.root, ignore_errors=True)

    # the browser's side ------------------------------------------------------------------------------------
    def req(self, method: str, path: str, body=None, raw: bool = False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": f"localhost:{self.port}", "X-Baabaa-Client": "browser"}
        if body is not None:
            h["Content-Type"] = "application/json"
        if self.cookie:
            h["Cookie"] = self.cookie
        if self.csrf:
            h["X-CSRF-Token"] = self.csrf
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        if r.status >= 400:
            raise SystemExit(f"{method} {path}: {r.status} {data[:300].decode(errors='replace')}")
        if method == "POST" and path == "/api/setup":
            self.cookie = r.getheader("Set-Cookie").split(";")[0]
        return data.decode() if raw else json.loads(data) if data else None

    def listen(self) -> None:
        """Read the live event stream, as a browser window would, into self.events."""
        def run():
            c = http.client.HTTPConnection("127.0.0.1", self.port)
            c.request("GET", "/api/events", headers={"Host": f"localhost:{self.port}", "Cookie": self.cookie})
            event = None
            try:
                for raw in c.getresponse():
                    line = raw.decode().rstrip("\n")
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: ") and event:
                        self.events.append((event, json.loads(line[6:])))
                        event = None
            except (OSError, ValueError):
                pass
        threading.Thread(target=run, daemon=True).start()

    def wait(self, what: str, test, seconds: float = 120):
        end = time.time() + seconds
        while time.time() < end:
            got = test()
            if got:
                return got
            time.sleep(0.03)
        raise SystemExit(f"Timed out waiting for {what}.")

    # setting the scene -----------------------------------------------------------------------------------------
    def prepare(self) -> None:
        d = self.req("POST", "/api/setup", {"name": "guest", "display_name": "Guest", "token": "preview"})
        self.csrf = d["csrf"]
        self.listen()
        self.req("POST", "/api/models/sync", {})
        from fake_ollama import CHAT_MODEL
        self.ollama.script(list(self.script["FIT_REPLIES"]))
        self.req("POST", "/api/models/test", {"model": CHAT_MODEL})

        def fitted():
            m = next((x for x in self.req("GET", "/api/models").get("installed", []) if x["name"] == CHAT_MODEL), {})
            return (m.get("fit") or {}).get("fits") is True
        self.wait("the fit test", fitted)
        self.req("POST", "/api/models/approve", {"model": CHAT_MODEL, "approved": True})
        self.req("POST", "/api/folders/trust", {"path": str(self.work)})
        self.req("PATCH", "/api/network", {"mode": "local"})   # this computer only: no address of this machine is listed
        for text in ("Keeps a small flock of Shetland sheep.", "Prefers metric units and short answers."):
            self.req("POST", "/api/memory", {"text": text})

    def record(self, conv: dict) -> dict:
        """Play one conversation through the server; what a window sees of it, and how it ends."""
        self.ollama.script([dict(r) for r in conv["replies"]])
        created = self.req("POST", "/api/conversations", {"folder": str(self.work)} if conv.get("folder") else {})["conversation"]
        cid, mark = created["id"], len(self.events)
        self.req("POST", f"/api/conversations/{cid}/messages", {"text": conv["prompt"]})
        answered = set()

        def over():
            mine = self.events[mark:]
            for kind, d in mine:
                if kind == "pending" and d.get("conv_id") == cid and d["id"] not in answered:
                    answered.add(d["id"])
                    self.req("POST", f"/api/pending/{d['id']}", {"decision": "allow_once"})
            idle = any(k == "turn" and d.get("conv_id") == cid and d.get("state") == "idle" for k, d in mine)
            titled = any(k == "conv" and d["conversation"]["id"] == cid and d["conversation"].get("title") for k, d in mine)
            return idle and titled
        self.wait(f"the reply to {conv['prompt']!r}", over)
        time.sleep(0.3)
        events = canonical([[k, d] for k, d in self.events[mark:]
                            if d.get("conv_id") == cid or (k == "conv" and d["conversation"]["id"] == cid) or k == "queue"])
        final = self.req("GET", f"/api/conversations/{cid}")
        last = final["thread"][-1]
        if self.ollama.replies or last["status"] != "ok" or any(b.get("type") == "error" for b in last["blocks"]):
            raise SystemExit(f"{conv['key']}: the conversation did not go as written: status {last['status']}, "
                             f"{len(self.ollama.replies)} replies unused, blocks {[b.get('type') for b in last['blocks']]}")
        listed = next(c for c in self.req("GET", "/api/conversations?limit=200")["conversations"] if c["id"] == cid)
        out = {"key": conv["key"], "prompt": conv["prompt"], "home": bool(conv.get("home")), "folder": bool(conv.get("folder")),
               "shown": bool(conv.get("shown")), "created": created, "events": events, "final": final, "listed": listed}
        text = json.dumps(out).replace('"Test title"', json.dumps(conv["title"]))  # the stand-in names every chat alike
        return timed(json.loads(text))

    def snapshot(self, recordings: list[dict]) -> tuple[dict, dict]:
        """What the server answers to the requests a page makes, and each artifact's own page."""
        for r in recordings:
            if not r["shown"]:
                self.req("DELETE", f"/api/conversations/{r['created']['id']}")
        pid = self.req("POST", "/api/projects", {"name": "Flock handbook", "instructions":
                                                 "Answer as an experienced shepherd would: practical, in metric units."})["project"]["id"]
        self.req("POST", f"/api/projects/{pid}/text", {"name": "lambing-notes.md", "text": LAMBING_NOTES})
        self.wait("the project's document", lambda: all(
            f["status"] == "ready" for f in self.req("GET", f"/api/projects/{pid}").get("files", [])) or None)
        paths = SNAPSHOT + [f"/api/projects/{pid}"]
        frames = {}
        for r in recordings:
            for a in r["final"].get("artifacts") or []:
                paths.append(f"/api/artifacts/{a['id']}")
                frames[f"{a['id']}-{a['version']}"] = self.req("GET", f"/artifact-frame/{a['id']}?v={a['version']}", raw=True)
        snap = {p: self.req("GET", p) for p in paths}
        # the page of a baabaa that was installed, has Ollama running and is up to date
        snap["/api/status"]["ollama"] = True
        snap["/api/update"].update(kind="installed", disabled=None, checked_ms=int(time.time() * 1000))
        return snap, frames

    def renames(self) -> list[tuple[str, str]]:
        """This machine's names for things, and what the preview calls them (longest first)."""
        from fake_ollama import CHAT_MODEL, EMBED_MODEL
        return [(str(self.work), f"{GUEST}/projects/{self.script['PROJECT_NAME']}"), (str(self.projects), f"{GUEST}/projects"),
                (str(self.home), f"{GUEST}/.local/share/baabaa"), (str(self.root), f"{GUEST}/.cache"),
                (self.ollama_url, "http://127.0.0.1:11434"), (f"127.0.0.1:{self.port}", "localhost:8443"),
                (f"localhost:{self.port}", "localhost:8443"), (self.csrf, "preview"),
                (CHAT_MODEL, MODEL), (EMBED_MODEL, "scripted-embed:1m"), ('"family": "fake"', '"family": "scripted"')]


LAMBING_NOTES = """# Lambing notes

- Ewes lamb in April, about 147 days after tupping.
- Bring ewes close to the barn two weeks before the first due date.
- A lamb should stand and suck within the first hour.
- Keep iodine for navels, and colostrum in the freezer.
"""


def canonical(events: list) -> list:
    """The server sends each block as it is at the moment of sending, and a recording made at full speed catches
    most blocks already finished. This puts back what a window sees at reading speed: a block starts empty, its
    text arrives, a thought then ends, and a tool is running until its last word."""
    last = {(d["msg_id"], d["index"]): i for i, (kind, d) in enumerate(events) if kind == "msg.block"}
    seen, out = set(), []
    for i, (kind, d) in enumerate(events):
        if kind == "msg.block":
            key, b = (d["msg_id"], d["index"]), dict(d["block"])
            if b["type"] in ("thinking", "text") and key not in seen:
                b["text"] = ""
                b.pop("ms", None)
            elif b["type"] == "tool" and last[key] != i and b.get("status") not in ("pending", "checking", "waiting", "running"):
                b = {k: v for k, v in b.items() if k in ("type", "id", "name", "args", "decision", "approval_id")}
                b["status"] = "running"
            seen.add(key)
            d = {**d, "block": b}
        elif kind == "msg.new" and d["message"]["role"] == "assistant" and d["message"]["status"] == "streaming":
            d = {**d, "message": {**d["message"], "blocks": []}}
        out.append([kind, d])
    return out


def timed(rec: dict) -> dict:
    """Recordings are made at full speed. The preview plays text at RATE tokens a second, so a thought is given
    the time it will take there, and a reply the sum of its parts."""
    def patch(blocks: list, meta: dict | None = None) -> None:
        total = chars = 0
        for b in blocks:
            text = b.get("text") or ""
            if b.get("type") == "thinking" and "ms" in b:
                b["ms"] = max(1000, round(len(text) / 4 / RATE * 1000))
                total += b["ms"]
            elif b.get("type") == "text":
                total += round(len(text) / 4 / RATE * 1000)
            elif b.get("type") == "tool":
                total += (b.get("duration_ms") or 0) + 1200
            chars += len(text)
        if meta is not None and meta.get("duration_ms") is not None:
            meta["duration_ms"], meta["output_tokens"] = total + 900, max(1, round(chars / 4))
    for kind, d in rec["events"]:
        if kind == "msg.block":
            patch([d["block"]])
        elif kind in ("msg.new", "msg.done"):
            patch(d["message"]["blocks"], d["message"].get("meta"))
    for m in rec["final"]["thread"]:
        patch(m["blocks"], m.get("meta"))
    return rec


def folders(script: dict) -> dict:
    """What the folder chooser shows: one place, with the sample project in it."""
    base, work = f"{GUEST}/projects", f"{GUEST}/projects/{script['PROJECT_NAME']}"
    top = {"path": base, "parent": None, "trusted": False, "folders": [{"name": script["PROJECT_NAME"], "path": work}]}
    return {"": top, base: top, work: {"path": work, "parent": base, "trusted": True, "folders": []}}


# what must not be in the result ---------------------------------------------------------------------------------
def machine_marks() -> list[tuple[str, re.Pattern]]:
    """Things that would name the machine that built the site."""
    marks = [("this machine's home folder", re.compile(re.escape(str(Path.home()))))]
    user, host = getpass.getuser(), socket.gethostname()
    if user and user not in ("root", "runner", "guest"):
        marks.append(("this machine's user name", re.compile(rf"(?<![\w-]){re.escape(user)}(?![\w-])")))
    if host and host != "localhost":
        marks.append(("this machine's host name", re.compile(rf"(?<![\w.-]){re.escape(host)}(?![\w-])", re.I)))
    try:
        from baabaa.server.lan import interfaces
        for i in interfaces():
            addr = str(i.get("ip") or "")
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", addr) and not addr.startswith("127."):
                marks.append(("an address of this machine", re.compile(rf"(?<![\d.]){re.escape(addr)}(?![\d.])")))
    except Exception:  # noqa: BLE001 - no interfaces to read is no reason to stop
        pass
    try:
        import release
        if release.PRIVATE_WORDS.exists():
            for line in release.PRIVATE_WORDS.read_text().splitlines():
                if line.strip() and not line.startswith("#"):
                    marks.append(("a private word", re.compile(line.strip(), re.I)))
    except ImportError:
        pass
    return marks


def leaks(out: Path, tmp_root: str | None = None) -> list[str]:
    marks = machine_marks()
    if tmp_root:
        marks.append(("the build's own folder", re.compile(re.escape(tmp_root))))
    found = []
    for f in sorted(p for p in out.rglob("*") if p.is_file()):
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for what, pattern in marks:
            m = pattern.search(text)
            if m:
                found.append(f"{f.relative_to(out)}: {what} ({pattern.pattern[:40]})")
    return found


# writing the site ------------------------------------------------------------------------------------------------
def preview_page() -> str:
    """The app's own page, with addresses made relative and the stand-in for the server loaded first."""
    page = (WEB / "index.html").read_text()
    page = re.sub(r'(href|src)="/', r'\1="', page)
    page = re.sub(r'<link rel="manifest"[^>]*>\n', "", page)
    page = page.replace("<title>baabaa</title>", "<title>baabaa preview</title>\n"
                        f'<meta http-equiv="Content-Security-Policy" content="{APP_CSP}">\n<meta name="referrer" content="no-referrer">\n'
                        + share_tags("/preview/", "baabaa preview", "The baabaa interface with recorded replies: a chat, a coding session with "
                                     "approvals, artifacts, settings. Nothing here runs a model.", up="../"))
    page = page.replace('<link rel="stylesheet" href="css/app.css">',
                        '<link rel="stylesheet" href="css/app.css">\n<link rel="stylesheet" href="preview.css">')
    page = page.replace('<script type="module" src="js/app.js"></script>',
                        '<script src="preview-data.js"></script>\n<script src="preview.js"></script>\n'
                        '<script type="module" src="js/app.js"></script>')
    for needle in ("preview.js", "preview.css", "Content-Security-Policy"):
        if needle not in page:
            raise SystemExit(f"baabaa/web/index.html changed: the preview page could not be made ({needle}).")
    return page


def framed(doc: str) -> str:
    """An artifact's page as a file of its own: the policy the real server sends as a header goes inside it."""
    meta = f'<meta http-equiv="Content-Security-Policy" content="{ARTIFACT_CSP}">'
    return re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + meta, doc, count=1) if re.search(r"<head[^>]*>", doc) else meta + doc


def share_tags(path: str, title: str, description: str, up: str = "") -> str:
    """What a shared link shows (Open Graph and Twitter cards), the canonical address and the icons."""
    site = f"https://{DOMAIN}"
    return "\n".join([
        f'<link rel="canonical" href="{site}{path}">',
        '<meta property="og:type" content="website">', '<meta property="og:site_name" content="baabaa">',
        f'<meta property="og:title" content="{title}">', f'<meta property="og:description" content="{description}">',
        f'<meta property="og:url" content="{site}{path}">', f'<meta property="og:image" content="{site}/og.png">',
        '<meta property="og:image:width" content="1200">', '<meta property="og:image:height" content="630">',
        '<meta property="og:image:alt" content="baabaa: your own assistant and coding agent, on your own GPU.">',
        '<meta name="twitter:card" content="summary_large_image">', f'<meta name="twitter:title" content="{title}">',
        f'<meta name="twitter:description" content="{description}">', f'<meta name="twitter:image" content="{site}/og.png">',
        f'<link rel="apple-touch-icon" href="{up}preview/img/apple-touch-icon.png">', '<meta name="theme-color" content="#2f7d6d">'])


def build(out: Path, domain: str | None = None, quiet: bool = False) -> dict:
    say = (lambda *a: None) if quiet else print
    script = runpy.run_path(str(SITE / "script.py"))
    from baabaa import __version__
    with Stage(script) as stage:
        stage.prepare()
        recordings = []
        for conv in script["CONVERSATIONS"]:
            recordings.append(stage.record(conv))
            say(f"  recorded: {conv['prompt']}")
        snapshot, frames = stage.snapshot(recordings)
        data = {"version": __version__, "built_ms": int(time.time() * 1000), "rate": RATE, "model": MODEL, "snapshot": snapshot,
                "recordings": recordings, "folders": folders(script), "frames": sorted(frames)}
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        for old, new in stage.renames():
            text = text.replace(old, new)
            frames = {k: v.replace(old, new) for k, v in frames.items()}
        tmp_root = str(stage.root)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(WEB, out / "preview", ignore=shutil.ignore_patterns("sw.js", "manifest.webmanifest"))
    (out / "preview" / "index.html").write_text(preview_page())
    (out / "preview" / "preview-data.js").write_text("window.BAABAA_PREVIEW = " + text.replace("</", "<\\/") + ";\n")
    for name in ("preview.js", "preview.css"):
        shutil.copy(SITE / name, out / "preview" / name)
    (out / "preview" / "artifact-frame").mkdir()
    for key, doc in frames.items():
        (out / "preview" / "artifact-frame" / f"{key}.html").write_text(framed(doc))
    for name in ("index.html", "site.css", "site.js"):
        page = (SITE / name).read_text()
        page = page.replace("{{share}}", share_tags("/", "baabaa: your own assistant and coding agent, on your own GPU",
                                                    "A self-hosted assistant and coding agent on local models, for your computer or your network. "
                                                    "Chat and code in one place, with memory, projects, artifacts and a sandbox. No dependencies."))
        (out / name).write_text(page.replace("{{version}}", __version__).replace("{{csp}}", APP_CSP))
    shutil.copy(SITE / "og.png", out / "og.png")
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: https://{DOMAIN}/sitemap.xml\n")
    (out / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                     + "".join(f"  <url><loc>https://{DOMAIN}{p}</loc></url>\n" for p in ("/", "/docs/", "/preview/")) + "</urlset>\n")
    (out / "docs").mkdir()
    docs = (SITE / "docs.html").read_text().replace("{{csp}}", APP_CSP)
    docs = docs.replace("{{share}}", share_tags("/docs/", "baabaa documents", "The tutorial, the reference, the developer guide and every "
                                                "other document of baabaa, a self-hosted assistant and coding agent on local models.", up="../"))
    (out / "docs" / "index.html").write_text(docs)
    (out / "docs" / "src" / "docs").mkdir(parents=True)
    names = DOCUMENTS + sorted(f"docs/{p.name}" for p in (ROOT / "docs").glob("*.md"))
    for name in names:
        shutil.copy(ROOT / name, out / "docs" / "src" / name)
    (out / "docs" / "src" / "index.json").write_text(json.dumps(names))
    (out / ".nojekyll").write_text("")
    if domain:
        (out / "CNAME").write_text(domain + "\n")
    found = leaks(out, tmp_root)
    if found:
        raise SystemExit("The site is not fit to publish:\n  " + "\n  ".join(found))
    files = [p for p in out.rglob("*") if p.is_file()]
    say(f"Built {out}: {len(files)} files, {sum(p.stat().st_size for p in files) / 1e6:.1f} MB, "
        f"{len(recordings)} recorded conversations. Nothing of this machine is in it.")
    return json.loads(text)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build baabaa's website (landing page, documents, preview) into a folder.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--out", default=str(ROOT / "dist" / "site"))
        p.add_argument("--domain", help="write a CNAME file for GitHub Pages")
        if name == "serve":
            p.add_argument("--port", type=int, default=8900)
    args = ap.parse_args()
    out = Path(args.out).resolve()
    build(out, args.domain)
    if args.cmd == "serve":
        from functools import partial
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        print(f"http://127.0.0.1:{args.port}/  (this computer only; Ctrl+C stops it)", flush=True)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(SimpleHTTPRequestHandler, directory=str(out)))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
