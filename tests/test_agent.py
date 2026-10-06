"""The agent end to end against a stand-in Ollama (fixtures/fake_ollama.py): no model, no GPU.

Covers projects (whole documents in the prompt, or excerpts found for each message), memory and the
search of earlier conversations.
"""

import asyncio
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))

from fake_ollama import CHAT_MODEL, EMBED_MODEL, FakeOllama  # noqa: E402

NOTES = ("Merino sheep grow very fine wool and need shearing once a year. " * 120 + "\n\n"
         + "Shearing happens in late spring, before lambing, when the weather is dry. " * 120 + "\n\n"
         + "Border collies help move the flock between the upper and lower pastures. " * 120)


class AgentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["BAABAA_HOME"] = self.tmp.name
        self.ollama = FakeOllama()
        url = self.ollama.start()
        from baabaa.app import App
        self.app = App(ollama_url=url)
        await self.app.registry.sync()
        for name, ctx in ((CHAT_MODEL, 16384), (EMBED_MODEL, 2048)):
            self.app.registry.set_fit(name, {"fits": True, "num_ctx": ctx, "max_ctx": ctx, "steps": []})
            self.app.registry.approve(name, True)

        async def vector_search(matrix, n, d, query, k, **kw):
            # Test stand-in for the GPU kernel (cuda.py): the same arithmetic, so no GPU is needed here.
            import array
            rows = array.array("f")
            rows.frombytes(matrix)
            scores = [sum(rows[i * d + j] * query[j] for j in range(d)) for i in range(n)]
            return sorted(((i, s) for i, s in enumerate(scores)), key=lambda x: -x[1])[:k]
        self.app.gateway.vector_search = vector_search
        self.account = self.app.accounts.create("owner", role="owner")
        self.store = self.app.stores.get(self.account["id"])

    async def asyncTearDown(self):
        await self.app.shutdown()
        self.ollama.stop()
        self.tmp.cleanup()

    async def turn(self, cid, text, replies):
        self.ollama.script(replies)
        before = len(self.ollama.requests)
        await self.app.agent.send(self.account, cid, text, [], "test")
        task = self.app.agent.turns[cid].task
        await asyncio.wait_for(task, 20)
        chats = [r for r in self.ollama.requests[before:] if r["path"] == "/api/chat" and r.get("messages")
                 and r["messages"][0]["role"] == "system"]  # the turn's requests, not conversation titles
        msg = self.store.message(self.store.conversation(cid)["leaf_id"])
        return msg, chats

    async def test_project_memory_and_past_chats(self):
        kn = self.app.knowledge
        project = self.store.create_project("Flock", instructions="Answer as a shepherd would.")
        kn.add_text(self.account["id"], project["id"], "notes.md", NOTES)
        await asyncio.wait_for(kn._tasks[(self.account["id"], project["id"])], 20)
        files = self.store.project_files(project["id"])
        self.assertEqual(files[0]["status"], "ready")
        self.assertEqual(files[0]["embed_model"], EMBED_MODEL)
        _, mode = kn.prompt(self.account["id"], project, 16384)
        self.assertEqual(mode, "search")  # too large to include whole at this context size

        conv = self.store.create_conversation(model=CHAT_MODEL, project_id=project["id"])
        msg, chats = await self.turn(conv["id"], "Please remember that my farm is in Otago. When is shearing?", [
            {"tool_calls": [{"name": "memory", "arguments": {"action": "add", "text": "The user's farm is in Otago."}}]},
            {"content": "Shearing is in late spring. I will remember Otago."}])
        kinds = [(b["type"], b.get("name"), b.get("status")) for b in msg["blocks"]]
        self.assertEqual(kinds[0], ("tool", "search_project", "done"))
        self.assertTrue(msg["blocks"][0]["auto"])
        self.assertIn("late spring", msg["blocks"][0]["output"])
        self.assertEqual(msg["blocks"][0]["sources"][0]["file"], "notes.md")
        self.assertIn(("tool", "memory", "done"), kinds)
        self.assertEqual(msg["blocks"][-1]["text"].strip(), "Shearing is in late spring. I will remember Otago.")
        system = chats[0]["messages"][0]["content"]
        self.assertIn("# Project: Flock", system)
        self.assertIn("Answer as a shepherd would.", system)
        names = [t["function"]["name"] for t in chats[0]["tools"]]
        for name in ("search_project", "memory", "past_chats"):
            self.assertIn(name, names)
        # the excerpts reach the model as a finished tool call at the start of its turn
        self.assertEqual(chats[0]["messages"][-1]["role"], "tool")
        mems = self.store.memories(project["id"])
        self.assertEqual([(m["text"], m["project_id"], m["source"]) for m in mems],
                         [("The user's farm is in Otago.", project["id"], "model")])

        # the next turn sees the memory in its instructions
        _, chats = await self.turn(conv["id"], "Thanks!", [{"content": "You're welcome."}])
        self.assertIn("The user's farm is in Otago.", chats[0]["messages"][0]["content"])

        # an earlier conversation outside the project, then a search for it from a new chat
        other = self.store.create_conversation(model=CHAT_MODEL, title="Llama fleece")
        await self.turn(other["id"], "How soft is alpaca and llama fleece?", [{"content": "Very soft."}])
        chat = self.store.create_conversation(model=CHAT_MODEL)
        msg, chats = await self.turn(chat["id"], "What did we say about llamas?", [
            {"tool_calls": [{"name": "past_chats", "arguments": {"query": "llama fleece"}}]}, {"content": "We said it is soft."}])
        tool = next(b for b in msg["blocks"] if b.get("name") == "past_chats")
        self.assertIn("Llama fleece", tool["output"])
        self.assertNotIn("# Project", chats[0]["messages"][0]["content"])
        self.assertNotIn("search_project", [t["function"]["name"] for t in chats[0]["tools"]])
        self.assertNotIn("Otago", chats[0]["messages"][0]["content"])  # a project's memory stays in the project

    async def test_scratch_folder_files_and_code(self):
        conv = self.store.create_conversation(model=CHAT_MODEL)
        msg, chats = await self.turn(conv["id"], "Make me a short report and a CSV.", [
            {"tool_calls": [{"name": "create_file", "arguments": {"path": "report.docx", "content": "# Flock\n\n- 120 ewes\n- 3 rams"}}]},
            {"tool_calls": [{"name": "bash", "arguments": {"command": "python3 -c \"open('counts.csv','w').write('kind,n\\newes,120\\n')\" && cat counts.csv"}}]},
            {"content": "Done: report.docx and counts.csv."}])
        names = [t["function"]["name"] for t in chats[0]["tools"]]
        self.assertIn("create_file", names)
        self.assertIn("bash", names)
        self.assertNotIn("bash_output", names)  # no background shells in a chat's scratch folder
        self.assertIn("private folder for this chat", chats[0]["messages"][0]["content"])
        tools = [b for b in msg["blocks"] if b["type"] == "tool"]
        self.assertEqual([(b["name"], b["status"], b["decision"]["layer"]) for b in tools],
                         [("create_file", "done", "sandbox"), ("bash", "done", "sandbox")])
        self.assertEqual([f["path"] for f in tools[0]["files"]], ["report.docx"])
        self.assertEqual([f["path"] for f in tools[1]["files"]], ["counts.csv"])
        self.assertIn("ewes,120", tools[1]["output"])
        scratch = self.app.paths.account(self.account["id"]) / "files" / "scratch" / conv["id"]
        import zipfile
        self.assertIn("word/document.xml", zipfile.ZipFile(scratch / "report.docx").namelist())
        # without code execution a chat has no file tools
        self.app.accounts.update_settings(self.account["id"], {"code_exec": False})
        self.account = self.app.accounts.get(self.account["id"])
        other = self.store.create_conversation(model=CHAT_MODEL)
        _, chats = await self.turn(other["id"], "Hello", [{"content": "Hi."}])
        self.assertNotIn("create_file", [t["function"]["name"] for t in chats[0]["tools"]])

    async def test_research(self):
        from baabaa.agent import web
        pages = {
            "https://a.example/wool": "Merino wool fibres are usually 17 to 24 micrometres in diameter. " * 10,
            "https://b.example/shearing": "Most flocks are shorn once a year, in spring before lambing. " * 10,
            "https://blocked.example/x": "should never be read " * 20,
            "https://c.example/broken": None,
        }

        async def fake_search(q, n=8):
            return [{"title": f"Page {u}", "url": u, "snippet": ""} for u in pages]

        async def fake_fetch(url, max_chars=20000):
            if pages[url] is None:
                raise web.FetchError("HTTP 500 from c.example")
            return {"url": url, "title": "T " + url.split("/")[2], "content": pages[url], "truncated": False}
        old_search, old_fetch = web.search, web.fetch
        web.search, web.fetch = fake_search, fake_fetch
        try:
            self.store.add_rule("deny", "WebFetch(domain:blocked.example)")
            conv = self.store.create_conversation(model=CHAT_MODEL)
            self.ollama.script([
                {"content": '{"queries": ["merino wool micron", "when to shear sheep"]}'},
                {"content": "- Merino fibres are 17-24 micrometres."},
                {"content": "- Shearing is once a year, in spring."},
                {"content": "Merino wool is fine [1] and sheep are shorn in spring [2].\n\nSources\n[1] a\n[2] b"}])
            before = len(self.ollama.requests)
            await self.app.agent.send(self.account, conv["id"], "How fine is Merino wool and when are sheep shorn?", [], "test",
                                      research=True)
            await asyncio.wait_for(self.app.agent.turns[conv["id"]].task, 20)
            msg = self.store.message(self.store.conversation(conv["id"])["leaf_id"])
            steps = [(b["name"], b["status"]) for b in msg["blocks"] if b["type"] == "tool"]
            self.assertEqual(steps[0], ("research_plan", "done"))
            self.assertEqual([x for x in steps if x[0] == "web_search"], [("web_search", "done")] * 2)
            fetched = [b["args"]["url"] for b in msg["blocks"] if b.get("name") == "web_fetch"]
            self.assertNotIn("https://blocked.example/x", fetched)  # the deny rule holds
            self.assertIn(("web_fetch", "error"), steps)            # the broken page is reported, not fatal
            self.assertEqual([x for x in steps if x[0] == "research_notes"], [("research_notes", "done")] * 2)
            self.assertIn("[1]", msg["blocks"][-1]["text"])
            chats = [r for r in self.ollama.requests[before:] if r["path"] == "/api/chat" and r.get("messages")
                     and not str(r["messages"][0].get("content", "")).startswith("Write a short title")]
            self.assertEqual(len(chats), 4)  # plan, two notes, report
            self.assertIn("17 to 24 micrometres", chats[1]["messages"][0]["content"])
            # a follow-up turn sees the report, not the research steps
            _, follow = await self.turn(conv["id"], "Thanks", [{"content": "You're welcome."}])
            history = follow[0]["messages"]
            self.assertFalse(any(m.get("tool_calls") for m in history))
            self.assertTrue(any("Merino wool is fine" in (m.get("content") or "") for m in history))
        finally:
            web.search, web.fetch = old_search, old_fetch

    async def test_web_pages_ask_in_a_folder(self):
        from baabaa.agent import web

        async def fake_fetch(url, max_chars=20000):
            return {"url": url, "title": "Docs", "content": "Shear in spring. " * 20, "truncated": False}
        old_fetch, web.fetch = web.fetch, fake_fetch
        try:
            folder = os.path.join(self.tmp.name, "farm")
            os.makedirs(folder)
            conv = self.store.create_conversation(model=CHAT_MODEL, mode="manual", folder=folder)
            page = {"name": "web_fetch", "arguments": {"url": "https://docs.example/shearing"}}
            self.ollama.script([{"tool_calls": [page]}, {"tool_calls": [page]}, {"content": "Shear in spring."}])
            await self.app.agent.send(self.account, conv["id"], "When do I shear? Check docs.example.", [], "test")
            turn = self.app.agent.turns[conv["id"]]
            for _ in range(200):           # the first page asks
                if self.app.agent.pending:
                    break
                await asyncio.sleep(0.02)
            ask = next(iter(self.app.agent.pending.values()))["payload"]
            self.assertEqual((ask["kind"], ask["tool"], ask["suggested_rule"]), ("approval", "web_fetch", "WebFetch(domain:docs.example)"))
            self.assertIn("asks first while working in a folder", ask["decision"]["reason"])
            self.app.agent.resolve(ask["id"], self.account, {"decision": "allow_always", "rule": ask["suggested_rule"]})
            await asyncio.wait_for(turn.task, 20)
            msg = self.store.message(self.store.conversation(conv["id"])["leaf_id"])
            reads = [b for b in msg["blocks"] if b.get("name") == "web_fetch"]
            self.assertEqual([b["status"] for b in reads], ["done", "done"])
            self.assertEqual([(b["decision"]["action"], b["decision"]["layer"]) for b in reads],
                             [("ask", "mode"), ("allow", "rule")])   # the second one needed no one
            self.assertIn({"kind": "allow", "pattern": "WebFetch(domain:docs.example)"},
                          [{"kind": r["kind"], "pattern": r["pattern"]} for r in self.store.rules()])
            # a chat without a folder reads pages without asking
            chat = self.store.create_conversation(model=CHAT_MODEL)
            msg, _ = await self.turn(chat["id"], "What does docs.example say?",
                                     [{"tool_calls": [{"name": "web_fetch", "arguments": {"url": "https://other.example/"}}]}, {"content": "Spring."}])
            read = [b for b in msg["blocks"] if b.get("name") == "web_fetch"][0]
            self.assertEqual((read["status"], read["decision"]["layer"]), ("done", "safe"))
        finally:
            web.fetch = old_fetch

    async def test_scheduled_task(self):
        from baabaa.scheduler import next_run, validate
        from baabaa.util import now_ms
        spec = validate({"kind": "daily", "at": "07:30"})
        sch = self.store.add_schedule("Morning note", "Say good morning to the flock.", spec, {}, now_ms() - 1000)
        self.ollama.script([{"content": "Good morning, flock!"}])
        ran = await self.app.scheduler.tick()
        self.assertEqual(ran, 1)
        sch = self.store.schedule(sch["id"])
        conv = self.store.conversation(sch["last_conv"])
        self.assertTrue(conv["title"].startswith("Morning note"))
        await asyncio.wait_for(self.app.agent.turns[conv["id"]].task, 20)
        reply = self.store.message(self.store.conversation(conv["id"])["leaf_id"])
        self.assertEqual(reply["blocks"][-1]["text"].strip(), "Good morning, flock!")
        self.assertEqual(sch["last_status"], "started")
        await asyncio.sleep(0.1)
        self.assertEqual(self.store.schedule(sch["id"])["last_status"], reply["status"])  # how the run ended
        self.assertGreater(sch["next_ms"], now_ms())             # the next day's run
        self.assertEqual(sch["next_ms"], next_run(spec, sch["last_ms"], sch["last_ms"]))
        self.assertEqual(await self.app.scheduler.tick(), 0)       # nothing else is due
        once = self.store.add_schedule("Once", "Hi", validate({"kind": "once", "date": "2030-01-01", "at": "10:00"}), {},
                                       now_ms() - 1)
        self.ollama.script([{"content": "Hello."}])
        await self.app.scheduler.tick()
        once = self.store.schedule(once["id"])
        self.assertFalse(once["enabled"])                          # a one-off turns itself off once it has run
        await asyncio.wait_for(self.app.agent.turns[once["last_conv"]].task, 20)

    def _image_model(self, edit=True, name="pix-test", seconds=None):
        """A registered image model served by the stand-in sd-server, approved as if its fit test passed."""
        tmp = self.tmp.name
        files = {}
        for k in ("diffusion_model", "llm", "vae"):
            files[k] = os.path.join(tmp, f"{name}-{k}.gguf")
            with open(files[k], "wb") as f:
                f.write(b"\0" * 1024)
        server = os.path.join(ROOT, "tests", "fixtures", "fake_sd_server.py")
        if sys.platform != "linux":
            self.skipTest("models outside Ollama run on Linux only")
        m = self.app.registry.add_sdcpp(name, {"server": server, **files, "edit": edit, "steps": 8})
        self.app.registry.set_fit(m["name"], {"fits": True, "num_ctx": 0, **({"seconds_1024": seconds} if seconds else {})})
        self.app.registry.approve(m["name"], True)

        class GpuSeesIt:  # the stand-in holds no VRAM; tell the check it holds the whole model
            def process_used(self, pid):
                return 10 * 2**20
        self.app.sdcpp.gpu = GpuSeesIt()
        return m

    async def test_images(self):
        self._image_model()
        # the composer's Image mode: no chat model involved
        conv = self.store.create_conversation(model=CHAT_MODEL)
        before = len(self.ollama.requests)
        await self.app.agent.send(self.account, conv["id"], "A sheep reading a newspaper, watercolour", [], "test",
                                  image={"shape": "landscape"})
        await asyncio.wait_for(self.app.agent.turns[conv["id"]].task, 20)
        msg = self.store.message(self.store.conversation(conv["id"])["leaf_id"])
        img = msg["blocks"][-1]
        self.assertEqual((img["type"], img["status"], img["width"], img["height"]), ("image", "done", 1216, 832))
        att = self.store.attachment(img["id"])
        self.assertTrue(att["meta"]["generated"])
        with open(att["path"], "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
        self.assertFalse([r for r in self.ollama.requests[before:] if r["path"] == "/api/chat" and r.get("messages")])
        self.assertEqual(self.store.conversation(conv["id"])["title"], "A sheep reading a newspaper, watercolour")
        row = self.app.stats.con.execute("SELECT kind, model, outcome FROM model_requests WHERE kind='image'").fetchone()
        self.assertEqual(tuple(row), ("image", "pix-test", "ok"))
        # the chat model asks for an edit of that image with the tool
        _, chats = await self.turn(conv["id"], "Now make it night time", [
            {"tool_calls": [{"name": "generate_image", "arguments": {"prompt": "the same sheep at night", "edit": True}}]},
            {"content": "Here it is at night."}])
        self.assertIn("generate_image", [t["function"]["name"] for t in chats[0]["tools"]])
        self.assertIn("[I made an image: A sheep reading a newspaper", str(chats[0]["messages"]))
        msg = self.store.message(self.store.conversation(conv["id"])["leaf_id"])
        imgs = [b for b in msg["blocks"] if b["type"] == "image"]
        self.assertEqual(len(imgs), 1)
        self.assertTrue(imgs[0]["edit"])
        self.assertEqual(self.store.attachment(imgs[0]["id"])["meta"]["edit_of"], img["id"])
        self.assertIsNone(self.app.sdcpp.running())  # stopped to make room for the chat model's reply

    async def test_image_model_choice(self):
        # the fastest approved model draws by default; an edit goes to a model that can edit
        self._image_model(edit=True, name="pix-slow-edits", seconds=90)
        self._image_model(edit=False, name="pix-fast", seconds=15)
        self.assertEqual(self.app.registry.image_model(), "pix-fast")
        self.assertEqual(self.app.registry.image_model("pix-slow-edits"), "pix-slow-edits")  # a person's choice
        conv = self.store.create_conversation(model=CHAT_MODEL)
        await self.app.agent.send(self.account, conv["id"], "A lamb in a field", [], "test", image={})
        await asyncio.wait_for(self.app.agent.turns[conv["id"]].task, 20)
        await self.app.agent.send(self.account, conv["id"], "Make it snow", [], "test", image={"edit": True})
        await asyncio.wait_for(self.app.agent.turns[conv["id"]].task, 20)
        rows = [tuple(r) for r in self.app.stats.con.execute("SELECT model, outcome FROM model_requests WHERE kind='image' ORDER BY ts_ms")]
        self.assertEqual(rows, [("pix-fast", "ok"), ("pix-slow-edits", "ok")])

    async def test_image_model_rules(self):
        m = self._image_model(edit=False)
        with self.assertRaises(ValueError):  # a CPU placement flag is refused when registering
            self.app.registry.add_sdcpp("pix-cpu", {**{k: m["source"][k] for k in ("server", "diffusion_model")},
                                                    "args": ["--vae-on-cpu"]})

        class GpuSeesNothing:
            def process_used(self, pid):
                return None
        self.app.sdcpp.gpu = GpuSeesNothing()
        conv = self.store.create_conversation(model=CHAT_MODEL)
        await self.app.agent.send(self.account, conv["id"], "A lamb", [], "test", image={})
        await asyncio.wait_for(self.app.agent.turns[conv["id"]].task, 20)
        msg = self.store.message(self.store.conversation(conv["id"])["leaf_id"])
        self.assertEqual(msg["status"], "error")
        self.assertIn("could not confirm that the model is on the GPU", msg["blocks"][-1]["text"])
        self.assertIsNone(self.app.sdcpp.running())  # refused and stopped

    async def test_dictation(self):
        import base64
        import struct
        from baabaa.server.api import Web
        self.ollama.audio = True
        self.app.registry.con.execute("UPDATE models SET digest='changed' WHERE name=?", (CHAT_MODEL,))
        await self.app.registry.sync()  # picks up the audio capability (and needs a new fit, as a new digest would)
        self.app.registry.set_fit(CHAT_MODEL, {"fits": True, "num_ctx": 16384, "max_ctx": 16384, "steps": []})
        self.app.registry.approve(CHAT_MODEL, True)
        self.assertEqual(self.app.registry.audio_model(), CHAT_MODEL)
        pcm = b"".join(struct.pack("<h", 0) for _ in range(16000))
        wav = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
               + b"data" + struct.pack("<I", len(pcm)) + pcm)

        class Req:
            body, query, account, window = wav, {"lang": "en"}, self.account, "browser"
        web = Web(self.app, guard=None, tls=False, port=0)
        import json
        if sys.platform != "linux":  # on a Mac, Ollama runs the audio encoder on the GPU itself
            self.ollama.script([{"content": "Count the lambs in the top field."}])
            resp = await web.transcribe(Req())
            self.assertEqual(json.loads(resp.body)["text"], "Count the lambs in the top field.")
            self.assertTrue([r for r in self.ollama.requests if r["path"] == "/api/chat" and r.get("messages")
                             and r["messages"][0].get("images")])  # the audio went to Ollama
            return
        # Ollama would run the audio encoder on the CPU, so baabaa runs the model's files itself with
        # Ollama's server program (here: a stand-in install around the fake llama-server)
        from baabaa import llamacpp
        root = os.path.join(self.tmp.name, "ollama-install")
        os.makedirs(os.path.join(root, "cuda_v12"))
        os.symlink(os.path.join(os.path.dirname(__file__), "fixtures", "fake_llama_server.py"), os.path.join(root, "llama-server"))
        open(os.path.join(root, "cuda_v12", "libggml-cuda.so"), "wb").close()
        blob = os.path.join(self.tmp.name, "sha256-model")
        with open(blob, "wb") as f:  # an empty GGUF (no metadata, no tensors) is enough for the stand-in
            f.write(b"GGUF" + struct.pack("<IQQ", 3, 0, 0) + b"\0" * 8)
        self.ollama.files[CHAT_MODEL] = [blob, blob]
        dirs, llamacpp.OLLAMA_DIRS = llamacpp.OLLAMA_DIRS, (root,)
        try:
            resp = await web.transcribe(Req())
        finally:
            llamacpp.OLLAMA_DIRS = dirs
        self.assertEqual(json.loads(resp.body)["text"], "Count the lambs in the top field.")
        self.assertFalse([r for r in self.ollama.requests if r["path"] == "/api/chat" and r.get("messages")
                          and r["messages"][0].get("images")])  # the audio never went to Ollama
        self.assertEqual(self.app.llamacpp.running()["encoder"], ["CUDA0"])
        Req.body = b"not audio" * 200
        with self.assertRaises(ValueError):
            await web.transcribe(Req())

    async def test_stuck_ollama(self):
        """Ollama stops answering for a model while the GPU idles: baabaa unloads it and asks once more."""
        from unittest import mock
        from baabaa import gateway as gw
        from baabaa.ollama import OllamaError

        async def ask():
            text = ""
            async for c in self.app.gateway.chat(model=CHAT_MODEL, messages=[{"role": "user", "content": "baa"}],
                                                 account_id=self.account["id"]):
                text += (c.get("message") or {}).get("content", "")
            return text

        unloads = lambda: [r for r in self.ollama.requests if r["path"] == "/api/generate" and r.get("keep_alive") == 0]
        with mock.patch.object(gw, "STUCK_CHECK_S", 0.05), mock.patch.object(gw, "STUCK_IDLE_S", 0.3), \
                mock.patch.object(self.app.gateway.gpu, "utilization", lambda: {"gpu": 0}):
            self.ollama.hang = 1
            self.ollama.script([{"content": "Still here."}])
            self.assertEqual((await ask()).strip(), "Still here.")
            self.assertEqual(len(unloads()), 1)
            self.ollama.hang = 2  # stuck again after the reload: a plain error, not an empty one
            with self.assertRaises(OllamaError) as cm:
                await ask()
            self.assertIn("stopped answering", str(cm.exception))
            self.assertEqual(len(unloads()), 3)

    async def test_unreadable_tool_call(self):
        """Ollama ends the reply when it cannot read the model's tool call (a whole page in a tool call, 2026-10-01):
        baabaa asks again, the model writes the page in its reply, and the page becomes an artifact."""
        from baabaa import gateway as gw
        unloads = lambda: [r for r in self.ollama.requests if r["path"] == "/api/generate" and r.get("keep_alive") == 0]
        page = ("<!DOCTYPE html>\n<html><head><title>Flock Lab</title></head>\n<body><h1>Flock Lab</h1></body>\n</html>")
        broken = {"thinking": "Use create_artifact.", "error": "XML syntax error on line 20: element <parameter> closed by </function>"}
        self.ollama.version = "0.35.0"
        conv = self.store.create_conversation(model=CHAT_MODEL)
        msg, chats = await self.turn(conv["id"], "make me a landing page", [
            broken, {"content": f"Here it is:\n\n```html\n{page}\n```\n\nIt opens beside the chat."}])
        self.assertEqual(msg["status"], "ok")
        self.assertIn("malformed", next(b["text"] for b in msg["blocks"] if b["type"] == "notice"))
        self.assertIn("could not be read", chats[1]["messages"][-1]["content"])
        self.assertIn("XML syntax error", chats[1]["messages"][-1]["content"])
        self.assertEqual(unloads(), [])  # Ollama 0.35 answers the next request: no reload needed
        text = next(b for b in msg["blocks"] if b["type"] == "text")
        arts = self.store.artifacts(conv["id"])
        self.assertEqual([(a["title"], a["kind"], a["version"]) for a in arts], [("Flock Lab", "html", 1)])
        self.assertEqual(self.store.artifact(arts[0]["id"])["content"], page)
        self.assertEqual(text["artifacts"][0]["id"], arts[0]["id"])
        self.assertTrue(text["text"][text["artifacts"][0]["start"]:].startswith("```html\n<!DOCTYPE html>"))

        # a revised page with the same title is the next version, and the model sees which artifact it is
        page2 = page.replace("<h1>Flock Lab</h1>", "<h1 style=\"font-size:4em\">Flock Lab</h1>")
        msg, chats = await self.turn(conv["id"], "make the heading bigger", [{"content": f"```html\n{page2}\n```"}])
        arts = self.store.artifacts(conv["id"])
        self.assertEqual([(a["title"], a["version"]) for a in arts], [("Flock Lab", 2)])
        self.assertEqual(self.store.artifact(arts[0]["id"])["content"], page2)
        history = "\n".join(m.get("content") or "" for m in chats[0]["messages"])
        self.assertIn(f"artifact {arts[0]['id']}", history)

        # the page rewritten with a tool, then repeated (a little differently) in the reply: no extra version
        page3 = page2.replace("4em", "5em")
        msg, _ = await self.turn(conv["id"], "bigger still", [
            {"tool_calls": [{"name": "rewrite_artifact", "arguments": {"id": arts[0]["id"], "content": page3}}]},
            {"content": f"Updated:\n\n```html\n{page3.replace('5em', '5.0em')}\n```"}])
        self.assertEqual(self.store.artifact(arts[0]["id"])["version"], 3)
        text = next(b for b in msg["blocks"] if b["type"] == "text")
        self.assertEqual((text["artifacts"][0]["id"], text["artifacts"][0]["version"]), (arts[0]["id"], 3))

        # an Ollama from before the fix stays stuck on the model after such an error: baabaa reloads it
        self.ollama.version = "0.30.7"
        self.app.gateway._version = ((), -gw.VERSION_TTL_MS)
        msg, _ = await self.turn(conv["id"], "and a footer", [broken, {"content": "Added."}])
        self.assertEqual(len(unloads()), 1)

        # three in a row: a plain message, not Ollama's
        msg, chats = await self.turn(conv["id"], "try again", [broken, broken, broken])
        self.assertEqual(msg["status"], "error")
        self.assertIn("malformed 3 times in a row", msg["blocks"][-1]["text"])
        self.assertEqual(len(chats), 3)

    async def test_checks_on_the_models_work(self):
        """What small models did in real conversations: a tool call written as text in a long reply (deepseek),
        stopping without a reply after thinking (qwen3.5:9b), escaped HTML in an artifact and saying a change
        was made when no tool ran (nemotron-3-nano:4b). baabaa catches each and sends the model back once."""
        notices = lambda m: [b["text"] for b in m["blocks"] if b["type"] == "notice"]
        conv = self.store.create_conversation(model=CHAT_MODEL)
        fake = ('Let me search:\n\n```json\n{\n  "tool": "web_search",\n  "query": "OpenClaw acquisition"\n}\n```\n\n'
                + "Based on the search results [source: web_search], the playbook is clear. " * 80)
        msg, chats = await self.turn(conv["id"], "do the discovery work first", [{"content": fake}, {"content": "I could not search."}])
        self.assertTrue(any("wrote a tool call as text" in n for n in notices(msg)))
        self.assertEqual(chats[0]["options"]["num_thread"], 1)  # Ollama 0.35's extra threads only spin
        self.assertIn("never describe results", chats[1]["messages"][-1]["content"])

        msg, chats = await self.turn(conv["id"], "what about his background?", [
            {"thinking": "Let me search for his background."}, {"content": "He founded PSPDFKit."}])
        self.assertTrue(any("stopped without replying" in n for n in notices(msg)))
        self.assertEqual(msg["blocks"][-1]["text"].strip(), "He founded PSPDFKit.")

        escaped = ('&lt;!DOCTYPE html&gt;\\n<html lang=\\"en\\"&gt;\\n&lt;head&gt;&lt;title&gt;Lab&lt;/title&gt;&lt;/head&gt;'
                   '\\n&lt;body&gt;Hi 🐑&lt;/body&gt;\\n&lt;/html&gt;')
        conv = self.store.create_conversation(model=CHAT_MODEL)
        await self.turn(conv["id"], "make me a landing page", [
            {"tool_calls": [{"name": "create_artifact", "arguments": {"title": "AI Lab Landing Page", "kind": "html", "content": escaped}}]},
            {"content": "Created."}])
        art = self.store.artifacts(conv["id"])[0]
        self.assertTrue(self.store.artifact(art["id"])["content"].startswith('<!DOCTYPE html>\n<html lang="en">\n<head>'))

        msg, chats = await self.turn(conv["id"], "now get rid of the emojis", [
            {"content": "I've regenerated the landing page without emojis."},
            {"tool_calls": [{"name": "rewrite_artifact", "arguments": {"id": "AI Lab Landing Page", "content": "<!DOCTYPE html>\n<p>Clean</p>"}}]},
            {"content": "Now it is done."}])
        self.assertTrue(any("said the work was done" in n for n in notices(msg)))
        self.assertIn("nothing was made or changed", chats[1]["messages"][-1]["content"])
        self.assertEqual(self.store.artifact(art["id"])["version"], 2)

        msg, _ = await self.turn(conv["id"], "show me what you have", [
            {"tool_calls": [{"name": "read_artifact", "arguments": {}}]},
            {"tool_calls": [{"name": "read_artifact", "arguments": {"id": art["id"][-8:]}}]},
            {"content": "That is the page."}])
        out = [b["output"] for b in msg["blocks"] if b.get("name") == "read_artifact"]
        self.assertIn("AI Lab Landing Page (html, version 2)", out[0])
        self.assertIn("<p>Clean</p>", out[1])
        self.assertEqual(notices(msg), [])  # an honest reply after real work: no checks fire

        msg, _ = await self.turn(conv["id"], "change the heading", [
            {"tool_calls": [{"name": "edit_file", "arguments": {"path": "index.html", "old_text": "Clean", "new_text": "Tidy"}}]},
            {"tool_calls": [{"name": "update_artifact", "arguments": {"id": art["id"], "old_text": "Clean", "new_text": "Tidy"}}]},
            {"content": "Changed the heading."}])
        edit = next(b for b in msg["blocks"] if b.get("name") == "edit_file")
        self.assertIn("Artifacts are not files", edit["output"])
        self.assertIn("<p>Tidy</p>", self.store.artifact(art["id"])["content"])

    async def test_chat_ignores_plan_mode(self):
        """A chat without a folder keeps all its tools in plan mode (the mode is hidden there), and a command the
        model starts in the background runs to the end, since a chat cannot read background output."""
        conv = self.store.create_conversation(model=CHAT_MODEL, mode="plan")
        msg, chats = await self.turn(conv["id"], "write a note", [
            {"tool_calls": [{"name": "bash", "arguments": {"command": "echo made > note.txt && cat note.txt", "background": True}}]},
            {"content": "Done: note.txt."}])
        tools = {t["function"]["name"]: t["function"] for t in chats[0]["tools"]}
        self.assertTrue({"write_file", "edit_file", "create_file"} <= set(tools))
        self.assertNotIn("propose_plan", tools)
        self.assertNotIn("background", tools["bash"]["parameters"]["properties"])
        bash = next(b for b in msg["blocks"] if b.get("name") == "bash")
        self.assertEqual((bash["status"], bash["output"].strip()), ("done", "made"))
        self.assertNotIn("shell_id", bash)

    async def test_fit_checks_tool_use(self):
        """The GPU fit test also checks how a model uses tools; a new test keeps the context the owner chose."""
        from unittest import mock
        self.ollama.script([
            {"content": "Sheep move slowly."},
            {"tool_calls": [{"name": "get_weather", "arguments": {"city": "Oslo"}}]},
            {"content": "144"},
            {"error": "XML syntax error on line 9: element <parameter> closed by </function>"}])
        roomy = {"total": 8 * 2**30, "used": 2**30, "free": 7 * 2**30}
        with mock.patch.object(self.app.jobs.gpu, "memory", lambda: roomy):
            job = self.app.jobs.start_fit(CHAT_MODEL, self.account["id"])
            await asyncio.wait_for(self.app.jobs.tasks[job], 60)
        m = self.app.registry.get(CHAT_MODEL)
        t = m["fit"]["tools"]
        self.assertEqual((t["passed"], t["of"]), (2, 3))
        self.assertEqual(t["checks"], {"calls": True, "answers": True, "long_argument": False})
        self.assertIn("XML syntax error", t["errors"]["long_argument"])
        self.assertEqual(m["fit"]["max_ctx"], 32768)
        self.assertEqual(m["num_ctx"], 16384)  # approved at 16k before the test: still 16k

    async def test_incognito_has_no_memory(self):
        self.store.add_memory("Likes tea.")
        conv = self.store.create_conversation(model=CHAT_MODEL, incognito=True)
        _, chats = await self.turn(conv["id"], "Hello", [{"content": "Hi."}])
        names = [t["function"]["name"] for t in chats[0].get("tools") or []]
        self.assertNotIn("memory", names)
        self.assertNotIn("past_chats", names)
        self.assertNotIn("Likes tea.", chats[0]["messages"][0]["content"])
        conv = self.store.create_conversation(model=CHAT_MODEL)
        _, chats = await self.turn(conv["id"], "Hello", [{"content": "Hi."}])
        self.assertIn("Likes tea.", chats[0]["messages"][0]["content"])

    async def test_what_the_thread_is_told(self):
        """The thread under a reply (web/js/thread.js, tui/thread.py) shows how long a thought took and how
        many replies are ahead in the GPU queue: both come from here."""
        sub = self.app.events.subscribe(self.account["id"], True, "test")

        def events():
            out = []
            while not sub.queue.empty():
                out.append(sub.queue.get_nowait())
            return out

        conv = self.store.create_conversation(model=CHAT_MODEL)
        self.ollama.delay = 0.06
        msg, _ = await self.turn(conv["id"], "Why is the sky blue?", [
            {"thinking": "Scattering. Keep it short.", "content": "Sunlight scatters in the air."}])
        thought = msg["blocks"][0]
        self.assertEqual(thought["type"], "thinking")
        self.assertTrue(40 <= thought["ms"] < 5000, thought)   # from its first word to the answer's first word
        told = [(e["data"]["index"], e["data"]["block"]["type"]) for e in events() if e["type"] == "msg.block"]
        # the thought's start, its end (timed) and only then the answer's block, so the thought folds first
        self.assertEqual(told, [(0, "thinking"), (0, "thinking"), (1, "text")])

        # a thought the reply ends on, or is stopped in, is timed too
        msg, _ = await self.turn(conv["id"], "And at night?", [{"thinking": "No sunlight to scatter."}, {"content": "It is dark."}])
        self.assertTrue(all("ms" in b for b in msg["blocks"] if b["type"] == "thinking"), msg["blocks"])

        # a reply that waits for the GPU tells its windows its place, and again when its turn comes
        other = self.store.create_conversation(model=CHAT_MODEL)
        events()
        self.ollama.delay = 0.15
        self.ollama.script([{"content": "one two three four five six seven eight"}, {"content": "after you"}])
        await self.app.agent.send(self.account, conv["id"], "first", [], "test")
        for _ in range(200):
            if self.app.gateway.queue.holder is not None:
                break
            await asyncio.sleep(0.01)
        await self.app.agent.send(self.account, other["id"], "second", [], "test")
        await asyncio.wait_for(asyncio.gather(*[t.task for t in list(self.app.agent.turns.values())]), 30)
        places = [e["data"].get("queue_position") for e in events() if e["type"] == "turn" and e["data"]["conv_id"] == other["id"]]
        self.assertIn(1, places)
        self.assertIsNone(places[places.index(1) + 1])
        self.assertEqual(self.store.message(self.store.conversation(other["id"])["leaf_id"])["status"], "ok")


if __name__ == "__main__":
    unittest.main()
