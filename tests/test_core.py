"""Unit tests that need no GPU, no Ollama and no network. Run: python3 -m unittest discover -s tests"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from baabaa import docs, library  # noqa: E402
from baabaa.accounts import AccountError, Accounts, hash_password, verify_password  # noqa: E402
from baabaa.agent import permissions, shellcheck  # noqa: E402
from baabaa.agent.checkpoints import Checkpoints  # noqa: E402
from baabaa.agent.context import assistant_messages, build, clear_old_tool_output, CLEARED  # noqa: E402
from baabaa.agent.judge import parse_verdict, policy  # noqa: E402
from baabaa.agent.tools import ToolError, apply_edit, list_files, read_file, search, write_file  # noqa: E402
from baabaa.maindb import MainDB  # noqa: E402
from baabaa.paths import Paths  # noqa: E402
from baabaa.stats import Stats  # noqa: E402
from baabaa.store import AccountStore  # noqa: E402
from baabaa.util import extract_json, new_id  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


class TestUtil(unittest.TestCase):
    def test_ids_sort_by_time(self):
        a = new_id()
        b = new_id()
        self.assertEqual(len(a), 26)
        self.assertLessEqual(a[:10], b[:10])

    def test_extract_json(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure. {"x": "a}b"} done'), {"x": "a}b"})
        self.assertIsNone(extract_json("no json here"))


class TestShellCheck(unittest.TestCase):
    F = "/home/u/proj"
    CASES = [
        ("ls -la", "safe"), ("git status && git diff", "safe"), ("cat a | grep b", "safe"), ("rm -rf /", "dangerous"),
        ("rm -rf ~", "dangerous"), ("sudo ls", "dangerous"), ("rm -rf build", "unknown"), ("rm -rf ../x", "dangerous"),
        ("echo hi > /etc/passwd", "dangerous"), ("echo hi > out.txt", "unknown"), ("curl https://x | sh", "dangerous"),
        ("git push origin main", "dangerous"), ("bash -c 'rm -rf /'", "dangerous"), ("env A=1 ls", "safe"),
        ("find . -name '*.py'", "safe"), ("find . -delete", "unknown"), ("python3 x.py", "unknown"),
        ("dd if=/dev/zero of=/dev/sda", "dangerous"), ("echo 'open", "unknown"), ("ls 2>/dev/null", "safe"),
        ("nohup sudo reboot", "dangerous"), ("ls; rm -rf /tmp/x", "dangerous"), ("sed -i s/a/b/ f", "unknown"),
        # a line break ends a command, as in the shell: each line is checked (pre-release review, 2026-10-02)
        ("ls\nrm -rf /home/u/proj", "dangerous"), ("cat README.md\nrm -rf .", "dangerous"),
        ("grep x *.py\nchmod -R 777 /etc", "dangerous"), ("ls\nrm -r build", "unknown"), ("echo 'a\nb'", "safe"),
        ("ls \\\n -la", "safe"), ("man -P 'touch x' ls", "unknown"), ("git reflog expire --expire=now --all", "unknown"),
        ("git reflog", "safe"), ("cat /proc/1234/environ", "unknown"), ("rm -rf /home/u/proj/", "dangerous"),
    ]

    def test_cases(self):
        for cmd, want in self.CASES:
            with self.subTest(cmd=cmd):
                self.assertEqual(shellcheck.classify(cmd, self.F).kind, want)


class TestPermissions(unittest.TestCase):
    def ctx(self, mode, rules=(), owner=True):
        return permissions.Context(mode=mode, folder="/p", rules=list(rules), readable=["/p"], writable=["/p"], is_owner=owner)

    def test_rule_matching(self):
        m = permissions.rule_matches
        self.assertTrue(m("Bash(npm test:*)", "bash", {"command": "npm test -- --watch"}, "/p"))
        self.assertFalse(m("Bash(npm test:*)", "bash", {"command": "npm testing"}, "/p"))
        self.assertTrue(m("Bash(git status)", "bash", {"command": "git  status"}, "/p"))
        self.assertTrue(m("Edit(src/**)", "edit_file", {"path": "src/a/b.py"}, "/p"))
        self.assertFalse(m("Edit(src/**)", "edit_file", {"path": "docs/a.md"}, "/p"))
        self.assertTrue(m("WebFetch(domain:example.com)", "web_fetch", {"url": "https://www.example.com/x"}, "/p"))
        self.assertTrue(m("Bash", "bash", {"command": "anything"}, "/p"))
        self.assertTrue(m("Write(x.txt)", "write_file", {"path": "x.txt"}, "/p"))

    def test_modes(self):
        d = permissions.decide
        edit = {"path": "a.py"}
        self.assertEqual(d("edit_file", "edit", edit, self.ctx("manual")).action, "ask")
        self.assertEqual(d("edit_file", "edit", edit, self.ctx("accept_edits")).action, "allow")
        self.assertEqual(d("edit_file", "edit", edit, self.ctx("plan")).action, "deny")
        self.assertEqual(d("edit_file", "edit", edit, self.ctx("auto")).action, "allow")
        self.assertEqual(d("edit_file", "edit", {"path": ".git/config"}, self.ctx("auto")).action, "ask")
        self.assertEqual(d("bash", "exec", {"command": "ls"}, self.ctx("auto")).action, "allow")
        self.assertEqual(d("bash", "exec", {"command": "npm test"}, self.ctx("auto")).action, "judge")
        self.assertEqual(d("bash", "exec", {"command": "sudo x"}, self.ctx("auto")).action, "ask")
        self.assertEqual(d("bash", "exec", {"command": "ls"}, self.ctx("manual")).action, "ask")
        self.assertEqual(d("bash", "exec", {"command": "ls"}, self.ctx("plan")).action, "allow")
        self.assertEqual(d("bash", "exec", {"command": "make"}, self.ctx("plan")).action, "deny")
        self.assertEqual(d("read_file", "read", {"path": "a.py"}, self.ctx("manual")).action, "allow")

    def test_rule_precedence_and_reach(self):
        rules = [{"kind": "allow", "pattern": "Bash"}, {"kind": "deny", "pattern": "Bash(rm:*)"}]
        c = self.ctx("manual", rules)
        self.assertEqual(permissions.decide("bash", "exec", {"command": "rm x"}, c).action, "deny")
        self.assertEqual(permissions.decide("bash", "exec", {"command": "ls"}, c).action, "allow")
        member = self.ctx("auto", [{"kind": "allow", "pattern": "Edit"}], owner=False)
        self.assertEqual(permissions.decide("write_file", "edit", {"path": "/etc/x"}, member).action, "deny")


class TestJudgeParse(unittest.TestCase):
    def test_parse_and_policy(self):
        v = parse_verdict('```json\n{"risk": "Low", "user_asked": "implicit", "decision": "run", "reason": "ok"}\n```')
        self.assertEqual((v["risk"], v["user_asked"], v["decision"]), ("low", "implied", "run"))
        self.assertTrue(policy(v))
        self.assertFalse(policy({"risk": "medium", "user_asked": "no", "decision": "run"}))
        self.assertTrue(policy({"risk": "medium", "user_asked": "explicit", "decision": "run"}))
        self.assertFalse(policy({"risk": "high", "user_asked": "explicit", "decision": "run"}))
        cut = parse_verdict('{"risk": "low", "user_asked": "explicit", "decision": "run", "reason": "The user as')
        self.assertEqual(cut["decision"], "run")


class TestAccounts(Tmp):
    def test_passwords(self):
        h = hash_password("sheep")
        self.assertTrue(verify_password("sheep", h))
        self.assertFalse(verify_password("goat", h))

    def test_accounts_sessions_grants(self):
        acc = Accounts(MainDB(os.path.join(self.dir, "m.db")))
        owner = acc.create("owner", "Owner", None, "owner")
        user = acc.create("kid", "Kid", "pw", "user")
        self.assertTrue(acc.check_password(owner["id"], None, "1.2.3.4"))
        self.assertFalse(acc.check_password(user["id"], "nope", "1.2.3.4"))
        self.assertTrue(acc.check_password(user["id"], "pw", "1.2.3.4"))
        token, csrf = acc.open_session(user["id"], "browser", "1.2.3.4")
        self.assertEqual(acc.session(token)["account"]["id"], user["id"])
        acc.close_session(token)
        self.assertIsNone(acc.session(token))
        with self.assertRaises(AccountError):
            acc.create("Bad Name!", "", None)
        with self.assertRaises(AccountError):
            acc.delete(owner["id"])  # the last owner
        os.makedirs(os.path.join(self.dir, "share", "sub"))
        acc.grant(user["id"], os.path.join(self.dir, "share"), "ro")
        self.assertEqual(acc.folder_access(user, os.path.join(self.dir, "share", "sub")), "ro")
        self.assertIsNone(acc.folder_access(user, self.dir))
        self.assertEqual(acc.folder_access(owner, "/"), "rw")


class TestStore(Tmp):
    def test_tree_branches_search(self):
        s = AccountStore(os.path.join(self.dir, "a.db"), "acct")
        c = s.create_conversation(title="t")
        u1 = s.add_message(c["id"], None, "user", [{"type": "text", "text": "hello sheep"}])
        a1 = s.add_message(c["id"], u1["id"], "assistant", [{"type": "text", "text": "baa"}])
        u2 = s.add_message(c["id"], a1["id"], "user", [{"type": "text", "text": "second"}])
        u2b = s.add_message(c["id"], a1["id"], "user", [{"type": "text", "text": "edited second"}])
        thread = s.thread(c["id"])
        self.assertEqual([m["id"] for m in thread], [u1["id"], a1["id"], u2b["id"]])
        self.assertEqual(thread[-1]["siblings"], [u2["id"], u2b["id"]])
        s.update_conversation(c["id"], leaf_id=s.descend(u2["id"]))
        self.assertEqual(s.thread(c["id"])[-1]["id"], u2["id"])
        self.assertEqual(s.search("sheep")[0]["msg_id"], u1["id"])
        fork = s.copy_thread(c["id"], a1["id"], "fork")
        self.assertEqual(len(s.thread(fork["id"])), 2)
        art = s.create_artifact(c["id"], a1["id"], "Doc", "markdown", "v1")
        s.update_artifact(art["id"], "v2")
        self.assertEqual(s.artifact(art["id"])["content"], "v2")
        self.assertEqual(s.artifact(art["id"], 1)["content"], "v1")
        s.delete_conversation(c["id"])
        self.assertIsNone(s.conversation(c["id"]))


class TestStats(Tmp):
    def test_record_summary_export_snapshot(self):
        st = Stats(os.path.join(self.dir, "s.db"))
        st.model_request(account_id="a", conv_id="c", kind="chat", model="m", prompt_tokens=10, output_tokens=5,
                         total_ms=100, tokens_per_s=50.0, ttft_ms=20, outcome="ok", gaps=[1, 2, 3])
        st.model_request(account_id="b", conv_id="d", kind="chat", model="m", prompt_tokens=1, output_tokens=1,
                         total_ms=10, outcome="stopped")
        st.tool_call(account_id="a", tool="bash", duration_ms=5, exit_code=1)
        st.event("login", "a", window="browser")
        s = st.summary(None, 0, 2**62, "model")
        self.assertEqual(s["total"]["requests"], 2)
        self.assertEqual(st.summary("a", 0, 2**62, "day")["total"]["requests"], 1)
        csv_text = b"".join(st.export("model_requests", "csv", "a", 0, 2**62)).decode()
        self.assertEqual(len(csv_text.strip().splitlines()), 2)
        rows = [json.loads(l) for l in b"".join(st.export("model_requests", "jsonl", None, 0, 2**62)).decode().splitlines()]
        self.assertEqual(json.loads(rows[0]["gaps"]), [1, 2, 3])
        dest = os.path.join(self.dir, "snap.db")
        st.snapshot(dest, "a")
        n = sqlite3.connect(dest).execute("SELECT COUNT(*) FROM model_requests").fetchone()[0]
        self.assertEqual(n, 1)


class TestTools(Tmp):
    def test_edit_cascade(self):
        text = "def f():\n    return 1\n\ndef g():\n    return 2\n"
        self.assertEqual(apply_edit(text, "return 1", "return 3")[1], "exact")
        new, how, _ = apply_edit(text, "def g():   \n    return 2", "def g():\n    return 4")
        self.assertEqual(how, "trailing-space")
        self.assertIn("return 4", new)
        new, how, _ = apply_edit(text, "def f():\n  return 1", "def f():\n  return 9")
        self.assertEqual(how, "indentation")
        self.assertIn("    return 9", new)
        with self.assertRaises(ToolError):
            apply_edit("a\na\n", "a", "b")
        with self.assertRaises(ToolError):
            apply_edit(text, "not there at all", "x")

    def test_file_tools(self):
        f = self.dir
        write_file({"path": "src/a.py", "content": "import os\nprint('baa')\n"}, f)
        with open(os.path.join(f, ".gitignore"), "w") as fh:
            fh.write("build/\n")
        os.makedirs(os.path.join(f, "build"))
        with open(os.path.join(f, "build", "x.py"), "w") as fh:
            fh.write("print('baa')\n")
        self.assertIn("     2\tprint('baa')", read_file({"path": "src/a.py"}, f))
        listing = list_files({"path": "."}, f)
        self.assertIn(os.path.join("src", "a.py"), listing)
        self.assertNotIn("build", listing)
        self.assertIn("src/a.py:2:", search({"pattern": "baa"}, f))


class TestCheckpoints(Tmp):
    def test_snapshot_restore(self):
        paths = Paths(os.path.join(self.dir, "data"))
        work = os.path.join(self.dir, "work")
        os.makedirs(work)
        with open(os.path.join(work, "a.txt"), "w") as fh:
            fh.write("one")
        ck = Checkpoints(paths)
        m = ck.snapshot("acct", work)
        with open(os.path.join(work, "a.txt"), "w") as fh:
            fh.write("two")
        with open(os.path.join(work, "new.txt"), "w") as fh:
            fh.write("new")
        r = ck.restore("acct", work, m)
        self.assertEqual(open(os.path.join(work, "a.txt")).read(), "one")
        self.assertFalse(os.path.exists(os.path.join(work, "new.txt")))
        self.assertEqual(r["removed"], ["new.txt"])


class TestContext(unittest.TestCase):
    def test_turn_to_messages(self):
        blocks = [{"type": "thinking", "text": "hm"}, {"type": "text", "text": "Let me look."},
                  {"type": "tool", "name": "read_file", "args": {"path": "a"}, "status": "done", "output": "x"},
                  {"type": "text", "text": "Done."}]
        msgs = assistant_messages(blocks)
        self.assertEqual([m["role"] for m in msgs], ["assistant", "tool", "assistant"])
        self.assertEqual(msgs[0]["tool_calls"][0]["function"]["name"], "read_file")

    def test_compaction_view_and_clearing(self):
        thread = [{"role": "user", "blocks": [{"type": "text", "text": "old"}]},
                  {"role": "compaction", "blocks": [{"type": "text", "text": "SUMMARY"}]},
                  {"role": "user", "blocks": [{"type": "text", "text": "new"}]}]
        msgs = build(thread, "SYS")
        self.assertEqual(msgs[0]["content"], "SYS")
        self.assertIn("SUMMARY", msgs[1]["content"])
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        self.assertEqual(msgs[-1]["content"], "new")
        big = [{"role": "system", "content": "s"}] + [{"role": "tool", "content": "x" * 5000} for _ in range(8)]
        cleared, n = clear_old_tool_output(big, 3000, 3.3)
        self.assertGreater(n, 0)
        self.assertEqual(cleared[1]["content"], CLEARED)
        self.assertEqual(cleared[-1]["content"], "x" * 5000)


class TestModelChecks(unittest.TestCase):
    """baabaa's checks on what small models write, with text from real conversations (2026-09-30 and 10-01)."""

    def test_claims_done(self):
        from baabaa.agent.loop import asks_for_work, claims_done
        said = [
            "I\u2019ve regenerated the landing page so it contains only essential, well-defined content.",
            "The landing\u2011page has been regenerated into a clean HTML file without any emojis.",
            "No\u2014there\u2019s no implication of fabrication. I *did* create the landing\u2011page for you:",
            "The landing\u2011page HTML has been saved **as an artifact in this conversation** (ID 01m3w8adzg).",
            "Here\u2019s exactly what we created and where you can get it:\n- **File name:** `landing.html`",
            "Your page is now ready.",
        ]
        for text in said:
            self.assertTrue(claims_done(text.replace("\u2019", "'")), text)
        for text in ["I'll rewrite the landing page so it is clean.", "Let me regenerate the page with clean code.",
                     "Shearing happens in late spring.", "I've thought about your question: the page would need a theme.",
                     "Once you approve, I will update the file.", "Here is how you could change the page yourself.",
                     "I've fixed the bug in your code:\n\n```python\nprint('baa')\n```",
                     "I've written the report below:\n\n## Flock\nSixty ewes.", "I updated my recommendation."]:
            self.assertFalse(claims_done(text), text)
        self.assertTrue(asks_for_work("now get rid of the emojis and redundant language"))
        self.assertTrue(asks_for_work("you talked about doing it but you never actually did it."))
        self.assertFalse(asks_for_work("where is it?"))

    def test_tool_call_written_as_text(self):
        from baabaa.agent import tools as T
        from baabaa.agent.loop import _looks_like_tool_call
        tools = {n: T.BY_NAME[n] for n in ("web_search", "web_fetch", "create_artifact")}
        fake = ('Let\'s perform a focused web search:\n\n```json\n{\n  "tool": "web_search",\n  "query": "wool grading case '
                'study"\n}\n```\n\n' + "Based on the search results [source: web_search], the playbook is clear. " * 100)
        self.assertGreater(len(fake), 4000)
        self.assertTrue(_looks_like_tool_call(fake, tools))
        self.assertTrue(_looks_like_tool_call('web_search({"query": "sheep"})', tools))
        self.assertTrue(_looks_like_tool_call("<tool_call>\n<function=web_search>", tools))
        self.assertFalse(_looks_like_tool_call('```json\n{"name": "search", "version": "1.0.0"}\n```', tools))
        nameless = ('**Creating the static landing page**\n\n```json\n{\n  "kind": "html",\n  "title": "Example Landing Page",\n'
                    '  "content": "<!DOCTYPE html><html><body>Hi</body></html>"\n}\n```')
        self.assertTrue(_looks_like_tool_call(nameless, tools))  # create_artifact's arguments, without its name
        self.assertFalse(_looks_like_tool_call('```json\n{"title": "Report", "pages": 3}\n```', tools))
        self.assertFalse(_looks_like_tool_call("I searched the web for sheep breeds.", tools))

    def test_unreadable_tool_call_errors(self):
        from baabaa.agent.loop import tool_call_unreadable
        self.assertTrue(tool_call_unreadable("XML syntax error on line 35: element <parameter> closed by </function>"))
        self.assertTrue(tool_call_unreadable("error parsing tool call: raw='{\"name\":', err=unexpected end of JSON input"))
        self.assertFalse(tool_call_unreadable("model requires more system memory (9.1 GiB) than is available"))

    def test_escaped_content(self):
        from baabaa.agent.tools import unescape_content
        page = ('&lt;!DOCTYPE html&gt;\\n<html lang=\\"en\\"&gt;\\n&lt;head&gt;\\n    &lt<meta charset=\\"UTF-8\\"&gt;'
                '\\n&lt;/head&gt;\\n&lt;body&gt;&lt;h1&gt;AI Lab&lt;/h1&gt;&lt;/body&gt;\\n&lt;/html&gt;')
        self.assertNotIn("\n", page)
        fixed = unescape_content(page, "html")
        self.assertTrue(fixed.startswith('<!DOCTYPE html>\n<html lang="en">\n<head>\n    <meta charset="UTF-8">'), fixed)
        self.assertIn("<h1>AI Lab</h1>", fixed)
        good = '<!DOCTYPE html>\n<p>Write &lt;div&gt; for a box.</p>\n'
        self.assertEqual(unescape_content(good, "html"), good)
        minified = '{"a": "one\\ntwo\\nthree\\nfour"}'
        self.assertEqual(unescape_content(minified, ".json"), minified)
        self.assertEqual(unescape_content("# Notes\\n\\n- one\\n- two", "markdown"), "# Notes\n\n- one\n- two")

    def test_page_fences(self):
        from baabaa.agent.loop import page_fences, page_title
        page = "<!DOCTYPE html>\n<html><head><title>Flock &amp; Fold</title></head>\n<body><h1>Hi</h1></body>\n</html>"
        text = f"Here it is:\n\n```html\n{page}\n```\n\nA snippet:\n\n```html\n<div>box</div>\n```\n\n```svg\n<svg xmlns=\"http://www.w3.org/2000/svg\"><circle r=\"4\"/></svg>\n```\n"
        found = page_fences(text)
        self.assertEqual([f["kind"] for f in found], ["html", "svg"])
        self.assertEqual(found[0]["content"], page)
        self.assertEqual(text[found[0]["start"]:found[0]["end"]], f"```html\n{page}\n```")
        self.assertEqual(page_title(page), "Flock & Fold")
        titled = page_fences('```html title="Landing page"\n<!doctype html>\n<p>x</p>\n```')
        self.assertEqual(titled[0]["title"], "Landing page")
        self.assertEqual(page_fences("```python\nprint(1)\n```"), [])

    def test_plan_mode_is_for_folders(self):
        from baabaa.agent import tools as T
        chat = {t.name for t in T.available(True, "plan", scratch=True)}
        self.assertTrue({"write_file", "edit_file", "create_file", "bash"} <= chat)
        self.assertNotIn("propose_plan", chat)
        folder = {t.name for t in T.available(True, "plan")}
        self.assertNotIn("write_file", folder)
        self.assertIn("propose_plan", folder)
        bash = next(s for s in T.schemas(T.available(True, "manual", scratch=True), scratch=True) if s["function"]["name"] == "bash")
        self.assertNotIn("background", bash["function"]["parameters"]["properties"])
        self.assertIn("background", T.BY_NAME["bash"].params)  # the shared definition is untouched

    def test_failed_calls_are_cut(self):
        from baabaa.agent import context
        page = "<html>" + "x" * 30000
        msgs = context.assistant_messages([
            {"type": "tool", "name": "edit_file", "status": "error", "output": "Error: index.html does not exist",
             "args": {"path": "index.html", "old_text": page, "new_text": page}},
            {"type": "tool", "name": "create_artifact", "status": "done", "output": "Created",
             "args": {"title": "Page", "kind": "html", "content": page}}])
        failed, done = msgs[0]["tool_calls"]
        self.assertLess(len(failed["function"]["arguments"]["old_text"]), 500)
        self.assertEqual(failed["function"]["arguments"]["path"], "index.html")
        self.assertEqual(done["function"]["arguments"]["content"], page)  # what was made stays whole

    def test_install_errors(self):
        from baabaa.models import pull_error
        self.assertIn("no model called bonsai", pull_error("bonsai", "pull model manifest: file does not exist"))
        self.assertIn("internet", pull_error("qwen3.5:9b", "Get \"https://registry.ollama.ai/v2/\": dial tcp: lookup registry.ollama.ai: no such host"))
        self.assertIsNone(pull_error("x:1b", "pull model manifest: sha256:ab401cd unexpected status"))


class TestHarmony(unittest.TestCase):
    """Harmony replies as llm-jp-4.1-8b-thinking writes them through Ollama 0.35 (2026-10-02): a space after each
    marker, which Ollama's own reader does not accept."""
    PLAIN = ('<|channel|> analysis<|message|> The user asks "What is 12 times 12?" Just give answer: 144. No extra text.'
             '<|end|><|start|> assistant<|channel|> final<|message|> 144')
    TOOL = ('<|channel|> analysis<|message|> I need to call get_weather with "Oslo".<|end|><|start|> assistant '
            'to=functions.get_weather<|channel|> commentary <|constrain|>  json<|message|> {"city": "Oslo"}')

    def read(self, text, pieces=None):
        from baabaa.harmony import Reader
        r = Reader()
        parts = []
        cuts = pieces or [len(text)]
        pos = 0
        for n in cuts:
            parts += r.feed(text[pos:pos + n])
            pos += n
        parts += r.feed(text[pos:]) + r.finish()
        joined = lambda kind: "".join(t for k, t in parts if k == kind)  # noqa: E731
        return joined("thinking"), joined("content"), r.calls

    def test_spaced_markers(self):
        thinking, content, calls = self.read(self.PLAIN)
        self.assertEqual(thinking, 'The user asks "What is 12 times 12?" Just give answer: 144. No extra text.')
        self.assertEqual((content, calls), ("144", []))
        thinking, content, calls = self.read(self.TOOL)
        self.assertTrue(thinking.startswith("I need to call"))
        self.assertEqual(content, "")
        self.assertEqual(calls, [{"function": {"name": "get_weather", "arguments": {"city": "Oslo"}}}])

    def test_any_split_and_gpt_oss_style(self):
        whole = self.read(self.TOOL)
        for size in (1, 2, 3, 5, 7, 11):
            self.assertEqual(self.read(self.TOOL, [size] * (len(self.TOOL) // size)), whole, size)
        tight = "<|channel|>analysis<|message|>Think.<|end|><|start|>assistant<|channel|>final<|message|>Answer."
        self.assertEqual(self.read(tight), ("Think.", "Answer.", []))
        self.assertEqual(self.read("A plain answer, no markers at all, which is still shown.")[1],
                         "A plain answer, no markers at all, which is still shown.")
        self.assertEqual(self.read("<|channel|> final<|message|> Use a<|b>c or <|x|> here.")[1], "Use a<|b>c or <|x|> here.")

    def test_bad_arguments_ask_again(self):
        from baabaa.agent.loop import tool_call_unreadable
        from baabaa.harmony import HarmonyError, Reader
        from baabaa.ollama import OllamaError
        r = Reader()
        r.feed('<|start|> assistant to=functions.get_weather<|channel|> commentary <|constrain|> json<|message|> {"city": ')
        with self.assertRaises(HarmonyError) as cm:
            r.finish()
        self.assertIsInstance(cm.exception, OllamaError)
        self.assertTrue(tool_call_unreadable(str(cm.exception)))

    def test_stream_and_detection(self):
        import asyncio
        from baabaa import harmony
        from baabaa.models import _info
        closed = []

        async def ollama():
            try:
                for i in range(0, len(self.TOOL), 9):
                    yield {"message": {"role": "assistant", "content": self.TOOL[i:i + 9]}, "done": False}
                yield {"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 30}
            finally:
                closed.append(True)

        async def run():
            return [c async for c in harmony.chunks(ollama())]
        out = asyncio.run(run())
        self.assertEqual("".join(c["message"]["content"] for c in out), "")
        self.assertTrue("".join(c["message"].get("thinking", "") for c in out).startswith("I need to call"))
        self.assertEqual(out[-1]["message"]["tool_calls"][0]["function"]["name"], "get_weather")
        self.assertEqual((out[-1]["done"], out[-1]["eval_count"], closed), (True, 30, [True]))
        show = {"template": "<|start|>assistant<|channel|>final<|message|>{{ .Content }}", "modelfile": "FROM x\nPARSER passthrough\n"}
        self.assertEqual(_info({}, show).get("output"), "harmony")
        self.assertIsNone(_info({}, {**show, "modelfile": "FROM x\nPARSER harmony\n"}).get("output"))


class TestDocs(unittest.TestCase):
    def _zip(self, files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in files.items():
                z.writestr(name, data)
        return buf.getvalue()

    def test_office(self):
        w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        docx = self._zip({"word/document.xml": f'<w:document xmlns:w="{w}"><w:body><w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:tab/><w:t>sheep</w:t></w:r></w:p></w:body></w:document>'})
        self.assertEqual(docs.extract("a.docx", docx), "Hello\tsheep")
        s = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        xlsx = self._zip({"xl/sharedStrings.xml": f'<sst xmlns="{s}"><si><t>name</t></si><si><t>a,b</t></si></sst>',
                          "xl/workbook.xml": f'<workbook xmlns="{s}"><sheets><sheet name="S1"/></sheets></workbook>',
                          "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{s}"><sheetData><row><c t="s"><v>0</v></c><c><v>3</v></c></row><row><c t="s"><v>1</v></c></row></sheetData></worksheet>'})
        self.assertEqual(docs.extract("a.xlsx", xlsx), '## Sheet: S1\nname,3\n"a,b"')

    def test_pdf(self):
        stream = b"BT /F1 12 Tf 72 700 Td (Hello \\(sheep\\)) Tj ET"
        pdf = (b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
               b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
               b"3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R >> endobj\n"
               b"4 0 obj << /Length " + str(len(stream)).encode() + b" >> stream\n" + stream + b"\nendstream endobj\n%%EOF")
        self.assertIn("Hello (sheep)", docs.extract("a.pdf", pdf))

    def test_classify(self):
        self.assertEqual(docs.classify("x.png", "image/png", b"\x89PNG"), "image")
        self.assertEqual(docs.classify("x.py", "", b"print(1)"), "text")
        self.assertEqual(docs.classify("x.bin", "", b"\x00\x01"), "binary")


class TestLibraryParsers(unittest.TestCase):
    PAGE = ('<li class="x"><a href="/library/sheepnet" class="group">'
            '<p class="max-w-lg break-words">A small &amp; fast model.</p>'
            '<span  class="inline-flex items-center rounded-md bg-indigo-50 text-indigo-600">tools</span>'
            '<span class="inline-flex items-center rounded-md bg-[#ddf4ff] text-blue-600">4b</span>'
            '<span class="flex items-center" title="Sep 1, 2026 5:04 PM UTC">Updated</span></a></li>')
    TAGS = ('<a href="/library/sheepnet:4b" class="md:hidden flex"><span>sheepnet:4b</span>'
            '<span> <span class="font-mono"> 2a654d98e6fb</span> • 3.4GB • 256K context window • Text, Image input • 6 months ago</span></a>')

    def test_parse(self):
        m = library.parse_library(self.PAGE)[0]
        self.assertEqual((m["name"], m["capabilities"], m["sizes"]), ("sheepnet", ["tools"], ["4b"]))
        self.assertTrue(m["updated"].startswith("2026-09-01"))
        t = library.parse_tags(self.TAGS)[0]
        self.assertEqual((t["tag"], t["digest"], t["size_gb"], t["context"], t["inputs"]), ("4b", "2a654d98e6fb", 3.4, 262144, "Text, Image"))
        self.assertEqual(library.size_to_b("e4b"), 4.0)
        self.assertEqual(library.size_to_b("300m"), 0.3)
        self.assertTrue(library.needs_newer("0.30.11", "0.30.7"))
        self.assertFalse(library.needs_newer("0.30.7", "0.30.11"))
        self.assertFalse(library.needs_newer(None, "0.30.7"))



class TestFindModels(unittest.TestCase):
    """Finding models without the network: a stand-in library, registry and GPU (8 GiB), no judge model."""
    LIB = [
        {"name": "sheepcoder", "description": "A coding model.", "capabilities": ["tools"], "sizes": ["4b", "9b", "32b"],
         "pulls": "2M", "updated": "2026-09-01T00:00:00+00:00"},
        {"name": "sheepvision", "description": "Sees images.", "capabilities": ["vision"], "sizes": ["8b"],
         "pulls": "90K", "updated": "2026-08-01T00:00:00+00:00"},
        {"name": "sheepembed", "description": "Text embeddings.", "capabilities": ["embedding"], "sizes": ["0.3b"],
         "pulls": "1M", "updated": "2026-07-01T00:00:00+00:00"},
        {"name": "sheephuge", "description": "A general model.", "capabilities": [], "sizes": ["70b"],
         "pulls": "5M", "updated": "2026-09-20T00:00:00+00:00"},
        {"name": "flock", "description": "A general model.", "capabilities": ["tools"], "sizes": ["9b"],
         "pulls": "1M", "updated": "2026-09-10T00:00:00+00:00"},
    ]

    def setUp(self):
        from types import SimpleNamespace
        from unittest import mock
        settings = {}

        async def sync():
            return None

        async def fetch_library():
            return [dict(x) for x in self.LIB]

        async def fetch_tags(name):
            return [{"tag": z, "context": 32768, "inputs": "Text", "digest": "abc123abc123"} for x in self.LIB if x["name"] == name for z in x["sizes"]]

        async def fetch_registry_info(name, tag):
            return {"weights": int(library.size_to_b(tag) * 0.6e9), "requires": "9.0.1" if name == "sheepvision" else "0.1.0"}

        async def version():
            return "0.30.7"

        self.settings = settings
        installed = [{"name": "flock:9b", "role": "chat", "param_b": 9.0, "runtime": "ollama", "digest": "0123456789abff",
                      "info": {"size": 6 * 10**9, "capabilities": ["completion"], "modified_at": "2026-01-01"}}]
        self.jobs = SimpleNamespace(
            registry=SimpleNamespace(sync=sync, all=lambda: installed, default_chat=lambda: None, get=lambda n: None),
            gpu=SimpleNamespace(memory=lambda: {"total": 8 * 2**30}), maindb=SimpleNamespace(get_setting=lambda k, d=None: settings.get(k, d)),
            ollama=SimpleNamespace(version=version), _progress=lambda *a: None)
        self.patches = [mock.patch.object(library, "fetch_library", fetch_library), mock.patch.object(library, "fetch_tags", fetch_tags),
                        mock.patch.object(library, "fetch_registry_info", fetch_registry_info)]
        for pt in self.patches:
            pt.start()

    def tearDown(self):
        for pt in self.patches:
            pt.stop()

    def run_search(self, query="", category=None):
        import asyncio
        from baabaa import scout
        return asyncio.run(scout.search(self.jobs, "job", None, query, category))

    def test_search(self):
        r = self.run_search("best model for coding")
        self.assertEqual([x["model"] for x in r["recommendations"]], ["sheepcoder:9b"])  # the largest size that fits
        self.assertEqual(r["scope"], "search")
        vision = self.run_search(category="vision")["recommendations"]
        self.assertEqual([(x["model"], x["requires"], x["ollama"]) for x in vision], [("sheepvision:8b", "9.0.1", "0.30.7")])
        self.assertIsNone(r["recommendations"][0]["requires"])  # 0.1.0 is older than what is installed
        self.assertEqual([x["model"] for x in self.run_search(category="embedding")["recommendations"]], ["sheepembed:0.3b"])
        self.assertEqual([x["model"] for x in self.run_search(category="small")["recommendations"]], ["sheepcoder:4b"])
        general = [x["model"] for x in self.run_search(category="general")["recommendations"]]
        self.assertNotIn("sheephuge:70b", general)  # never a model too large for the GPU
        self.assertNotIn("sheepembed:0.3b", general)
        self.settings["scout_dismissed"] = ["sheepcoder:9b"]
        self.assertEqual(self.run_search("coding")["recommendations"], [])  # a dismissed suggestion stays away

    def test_newer_versions_of_one_model(self):
        import asyncio
        from baabaa import scout
        r = asyncio.run(scout.scout(self.jobs, "job", None, only="flock:9b"))
        self.assertEqual((r["scope"], r["model"]), ("model", "flock:9b"))
        rebuild = [x for x in r["recommendations"] if x["kind"] == "rebuild"]
        self.assertEqual([scout.rec_key(x) for x in rebuild], ["flock:9b@abc123abc123"])  # the tag moved to a newer build
        self.settings["scout_dismissed"] = ["flock:9b@abc123abc123"]
        self.assertEqual(asyncio.run(scout.scout(self.jobs, "job", None, only="flock:9b"))["recommendations"], [])
        r = asyncio.run(scout.scout(self.jobs, "job", None, only="custom/model:1b"))
        self.assertIn("not one", r["note"])

if __name__ == "__main__":
    unittest.main()
