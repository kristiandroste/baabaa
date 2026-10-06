"""The website (tools/website.py): it builds, the preview's data is what the page expects, and nothing of the
machine that built it is in it. The build runs the real server against the stand-in for Ollama."""

import importlib.util
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from baabaa.sandbox import landlock, seatbelt  # noqa: E402

spec = importlib.util.spec_from_file_location("baabaa_website", ROOT / "tools" / "website.py")
website = importlib.util.module_from_spec(spec)
spec.loader.exec_module(website)

SANDBOX = seatbelt.available() if sys.platform == "darwin" else landlock.abi_version() >= 1


@unittest.skipUnless(SANDBOX, "the recorded coding conversation runs commands, which needs the tool sandbox")
class TestWebsite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "site"
        cls.data = website.build(cls.out, quiet=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_pages(self):
        for name in ("index.html", "site.css", "site.js", "docs/index.html", "docs/src/README.md", "docs/src/docs/TUTORIAL.md",
                     "docs/src/docs/DEVELOPING.md", "docs/src/LICENSE", "preview/index.html", "preview/preview.js",
                     "preview/preview-data.js", "preview/js/app.js", "preview/css/app.css", ".nojekyll"):
            self.assertTrue((self.out / name).is_file(), name)
        self.assertFalse((self.out / "CNAME").exists())           # only when a domain is given
        self.assertFalse((self.out / "preview" / "sw.js").exists())
        page = (self.out / "preview" / "index.html").read_text()
        self.assertLess(page.index("preview.js"), page.index("js/app.js"))   # the stand-in for the server loads first
        self.assertNotRegex(page, r'(href|src)="/')                 # the preview works from any folder
        for name in ("index.html", "docs/index.html", "preview/index.html"):
            html = (self.out / name).read_text()
            self.assertIn("script-src 'self'", html, name)          # no page runs a script from anywhere else
            self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)", name)   # and none has a script written into it
            self.assertNotIn("{{", html, name)
        names = json.loads((self.out / "docs" / "src" / "index.json").read_text())
        self.assertIn("docs/TUTORIAL.md", names)
        self.assertTrue(all((self.out / "docs" / "src" / n).is_file() for n in names))

    def test_recordings(self):
        d = self.data
        by = {r["key"]: r for r in d["recordings"]}
        script = website.runpy.run_path(str(website.SITE / "script.py"))
        self.assertEqual(sorted(by), sorted(c["key"] for c in script["CONVERSATIONS"]))
        for r in d["recordings"]:
            last = r["final"]["thread"][-1]
            self.assertEqual(last["status"], "ok", r["key"])
            self.assertTrue(r["final"]["conversation"]["title"], r["key"])
            self.assertEqual(r["final"]["conversation"]["model"], website.MODEL)
        fix = by["fix"]
        kinds = [k for k, _ in fix["events"]]
        self.assertEqual((kinds.count("pending"), kinds.count("pending.done")), (2, 2))   # two commands asked first
        tools = [b for b in fix["final"]["thread"][-1]["blocks"] if b["type"] == "tool"]
        self.assertEqual([b["name"] for b in tools], ["list_files", "bash", "read_file", "edit_file", "bash"])
        self.assertIn("FAILED (failures=1)", tools[1]["output"])    # the tests really ran, in the sandbox, and failed
        self.assertTrue(tools[4]["output"].rstrip().endswith("OK"))  # and passed after the edit
        self.assertIn("return len(set(tags))", tools[3]["diff"])
        self.assertEqual(fix["final"]["conversation"]["folder"], "/home/guest/projects/sheep-counter")
        # what a window sees at reading speed: a block starts empty, a thought ends once, a tool runs before it is done
        seen = set()
        for kind, e in fix["events"]:
            if kind != "msg.block":
                continue
            b, first = e["block"], (e["msg_id"], e["index"]) not in seen
            seen.add((e["msg_id"], e["index"]))
            if b["type"] in ("thinking", "text") and first:
                self.assertEqual((b["text"], b.get("ms")), ("", None))
        self.assertTrue(all(b["ms"] >= 1000 for b in fix["final"]["thread"][-1]["blocks"] if b["type"] == "thinking"))
        page = by["page"]["final"]["artifacts"][0]
        frame = (self.out / "preview" / "artifact-frame" / f"{page['id']}-{page['version']}.html").read_text()
        self.assertIn("connect-src 'none'", frame)                   # an artifact's page gets no network
        self.assertIn("sheep counted", frame)

    def test_snapshot(self):
        snap = self.data["snapshot"]
        self.assertEqual(snap["/api/me"]["account"]["display_name"], "Guest")
        self.assertEqual(snap["/api/me"]["csrf"], "preview")
        self.assertEqual((snap["/api/network"]["saved"], snap["/api/network"]["next_urls"]), ("local", None))
        self.assertEqual(snap["/api/status"]["gpu"]["name"], "Sample GPU, 16 GB")
        chat = next(m for m in snap["/api/models"]["models"] if m["role"] == "chat")
        self.assertEqual((chat["name"], chat["tool_use"]["passed"]), (website.MODEL, 3))
        self.assertEqual(self.data["folders"][""]["folders"][0]["name"], "sheep-counter")

    def test_nothing_of_this_machine(self):
        self.assertEqual(website.leaks(self.out), [])
        text = (self.out / "preview" / "preview-data.js").read_text()
        self.assertNotIn(tempfile.gettempdir() + "/baabaa-site-", text)
        self.assertNotRegex(text, r"127\.0\.0\.1:(?!11434)\d+")      # no port of the build's own servers
        # the check itself: a file that names this machine's home folder is caught
        copy = Path(self.tmp.name) / "copy"
        shutil.copytree(self.out, copy)
        (copy / "note.txt").write_text(f"built in {Path.home()}/somewhere")
        self.assertTrue(any("home folder" in line for line in website.leaks(copy)))


class TestWebsiteParts(unittest.TestCase):
    def test_events_in_reading_order(self):
        block = lambda index, b: ["msg.block", {"msg_id": "m", "index": index, "block": b}]  # noqa: E731
        events = website.canonical([
            ["msg.new", {"message": {"id": "m", "role": "assistant", "status": "streaming", "blocks": [{"type": "thinking"}]}}],
            block(0, {"type": "thinking", "text": "All of it.", "ms": 3}),
            block(0, {"type": "thinking", "text": "All of it.", "ms": 3}),
            block(1, {"type": "tool", "id": "t", "name": "bash", "args": {}, "status": "done", "output": "x", "duration_ms": 5}),
            block(1, {"type": "tool", "id": "t", "name": "bash", "args": {}, "status": "done", "output": "x", "duration_ms": 5}),
            block(2, {"type": "text", "text": "Done."}),
        ])
        blocks = [e[1]["block"] for e in events[1:]]
        self.assertEqual(events[0][1]["message"]["blocks"], [])
        self.assertEqual(blocks[0], {"type": "thinking", "text": ""})
        self.assertEqual(blocks[1]["ms"], 3)
        self.assertEqual((blocks[2]["status"], "output" in blocks[2]), ("running", False))
        self.assertEqual((blocks[3]["status"], blocks[3]["output"]), ("done", "x"))
        self.assertEqual(blocks[4]["text"], "")

    def test_reading_time(self):
        rec = {"events": [], "final": {"thread": [{"blocks": [
            {"type": "thinking", "text": "x" * 1520, "ms": 2}, {"type": "text", "text": "y" * 152}], "meta": {"duration_ms": 9}}]}}
        m = website.timed(rec)["final"]["thread"][0]
        self.assertEqual(m["blocks"][0]["ms"], 10000)                 # 380 tokens at 38 a second
        self.assertEqual(m["meta"], {"duration_ms": 10000 + 1000 + 900, "output_tokens": 418})

    def test_preview_page_and_frames(self):
        page = website.preview_page()
        self.assertIn('<script src="preview.js"></script>\n<script type="module" src="js/app.js"></script>', page)
        self.assertNotIn("manifest", page)
        framed = website.framed("<!doctype html><html><head><title>x</title></head><body></body></html>")
        self.assertRegex(framed, r"<head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'")
        self.assertTrue(website.framed("<p>bare</p>").startswith("<meta http-equiv"))
        self.assertTrue(re.search(r"frame-src 'self';", website.APP_CSP))


if __name__ == "__main__":
    unittest.main()
