"""HTTP server, LAN guard, API security and the sandbox. No Ollama or GPU needed."""

import asyncio
import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from baabaa.sandbox import helper_argv, landlock  # noqa: E402
from baabaa.server.lan import LanGuard  # noqa: E402


class TestLanGuard(unittest.TestCase):
    def test_clients_and_hosts(self):
        g = LanGuard(bind=["127.0.0.1"], networks=["192.168.50.0/24"], names=["sheep.lan"])
        self.assertTrue(g.allowed_client("127.0.0.1"))
        self.assertTrue(g.allowed_client("192.168.50.7"))
        self.assertTrue(g.allowed_client("::ffff:192.168.50.7"))
        self.assertFalse(g.allowed_client("8.8.8.8"))
        self.assertFalse(g.allowed_client("2001:db8::1"))
        self.assertTrue(g.allowed_host("localhost:8443"))
        self.assertTrue(g.allowed_host("sheep.lan"))
        self.assertFalse(g.allowed_host("evil.example"))
        self.assertFalse(g.allowed_origin("https://evil.example"))
        self.assertTrue(g.allowed_origin("https://localhost:8443"))


class TestApi(unittest.TestCase):
    """Runs the real server on an ephemeral port in a thread with its own event loop."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["BAABAA_HOME"] = cls.tmp.name
        os.environ["BAABAA_OLLAMA_URL"] = "http://127.0.0.1:9"  # nothing listens: model calls fail fast
        from baabaa.app import App
        from baabaa.server.api import Web
        from baabaa.server.http import Server
        import threading
        cls.loop = asyncio.new_event_loop()
        started = threading.Event()

        def run():
            asyncio.set_event_loop(cls.loop)
            cls.app = App()
            web = Web(cls.app, LanGuard(bind=["127.0.0.1"], networks=[]), tls=False, port=0, setup_token="tok")
            srv = Server(web)
            s = cls.loop.run_until_complete(srv.listen_tcp("127.0.0.1", 0))
            cls.port = s.sockets[0].getsockname()[1]
            started.set()
            cls.loop.run_forever()

        cls.thread = threading.Thread(target=run, daemon=True)
        cls.thread.start()
        started.wait(10)

    @classmethod
    def tearDownClass(cls):
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.thread.join(5)
        cls.tmp.cleanup()

    def req(self, method, path, body=None, cookie=None, csrf=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"localhost:{self.port}"}
        if body is not None:
            h["Content-Type"] = "application/json"
        if cookie:
            h["Cookie"] = cookie
        if csrf:
            h["X-CSRF-Token"] = csrf
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if data and r.getheader("content-type", "").startswith("application/json") else data), r

    def test_flow(self):
        status, _, _ = self.req("GET", "/api/health", host="evil.example")
        self.assertEqual(status, 421)
        status, d, _ = self.req("GET", "/api/me")
        self.assertEqual(status, 401)
        status, d, r = self.req("POST", "/api/setup", {"name": "owner", "display_name": "Owner"})
        self.assertEqual(status, 200)
        cookie = r.getheader("Set-Cookie").split(";")[0]
        self.assertIn("HttpOnly", r.getheader("Set-Cookie"))
        csrf = d["csrf"]
        status, _, _ = self.req("POST", "/api/setup", {"name": "again"})
        self.assertEqual(status, 409)
        # CSRF: a state change without the token is refused
        status, _, _ = self.req("POST", "/api/conversations", {}, cookie=cookie)
        self.assertEqual(status, 403)
        status, d, _ = self.req("POST", "/api/conversations", {"title": "hello"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        cid = d["conversation"]["id"]
        status, d, _ = self.req("GET", "/api/conversations", cookie=cookie)
        self.assertEqual([c["id"] for c in d["conversations"]], [cid])
        # a member cannot see the owner's conversation or owner-only endpoints
        status, d, _ = self.req("POST", "/api/accounts", {"name": "kid", "password": "pw"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        status, d, r = self.req("POST", "/api/login", {"name": "kid", "password": "wrong"})
        self.assertEqual(status, 401)
        status, d, r = self.req("POST", "/api/login", {"name": "kid", "password": "pw"})
        kid_cookie, kid_csrf = r.getheader("Set-Cookie").split(";")[0], d["csrf"]
        status, _, _ = self.req("GET", f"/api/conversations/{cid}", cookie=kid_cookie)
        self.assertEqual(status, 404)
        status, _, _ = self.req("GET", "/api/accounts", cookie=kid_cookie)
        self.assertEqual(status, 403)
        status, _, _ = self.req("POST", "/api/models/pull", {"model": "x"}, cookie=kid_cookie, csrf=kid_csrf)
        self.assertEqual(status, 403)
        status, _, _ = self.req("POST", "/api/conversations", {"folder": "/etc"}, cookie=kid_cookie, csrf=kid_csrf)
        self.assertEqual(status, 403)
        # API keys: bearer auth without CSRF; revocation takes effect at once
        status, d, _ = self.req("POST", "/api/me/keys", {"name": "script"}, cookie=cookie, csrf=csrf)
        token, kid = d["token"], d["key"]["id"]
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/conversations", headers={"Host": f"localhost:{self.port}", "Authorization": f"Bearer {token}"})
        r = c.getresponse(); body = json.loads(r.read()); c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(len(body["conversations"]), 1)
        self.req("DELETE", f"/api/me/keys/{kid}", cookie=cookie, csrf=csrf)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/conversations", headers={"Host": f"localhost:{self.port}", "Authorization": f"Bearer {token}"})
        r = c.getresponse(); r.read(); c.close()
        self.assertEqual(r.status, 401)
        # static app shell and security headers
        status, body, r = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))
        status, _, _ = self.req("GET", "/../../etc/passwd")
        self.assertIn(status, (200, 404))  # never the file: the SPA shell or 404


    def _owner(self):
        status, d, r = self.req("POST", "/api/setup", {"name": "owner", "display_name": "Owner"})
        if status != 200:
            status, d, r = self.req("POST", "/api/login", {"name": "owner"})
        return r.getheader("Set-Cookie").split(";")[0], d["csrf"]

    def test_models_find(self):  # after test_flow, which creates the owner
        cookie, csrf = self._owner()
        status, _, _ = self.req("POST", "/api/models/scout", {"category": "nope"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 400)
        status, _, _ = self.req("POST", "/api/models/scout", {"model": "missing:1b"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 404)
        status, d, _ = self.req("POST", "/api/models/scout/dismiss", {"key": "sheep:4b"}, cookie=cookie, csrf=csrf)
        self.assertEqual((status, d["dismissed"]), (200, ["sheep:4b"]))
        self.req("POST", "/api/models/scout/dismiss", {"key": "sheep:4b"}, cookie=cookie, csrf=csrf)  # remembered once
        self.req("POST", "/api/models/scout/dismiss", {"clear": "job1"}, cookie=cookie, csrf=csrf)
        status, d, _ = self.req("GET", "/api/models", cookie=cookie)
        self.assertEqual((d["scout"]["dismissed"], d["scout"]["cleared"]), (["sheep:4b"], "job1"))
        self.assertIn("coding", [c["id"] for c in d["scout"]["categories"]])

    def test_projects_memory_and_host_access(self):
        import time
        cookie, csrf = self._owner()
        status, d, _ = self.req("POST", "/api/projects", {"name": "Flock", "instructions": "Answer as a shepherd."},
                                cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        pid = d["project"]["id"]
        text = "Merino sheep grow fine wool. " * 40 + "\n\nShearing happens in spring, before lambing. " * 30
        status, d, _ = self.req("POST", f"/api/projects/{pid}/text", {"name": "notes.md", "text": text}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        fid = d["file"]["id"]
        for _ in range(50):
            status, d, _ = self.req("GET", f"/api/projects/{pid}", cookie=cookie)
            if d["files"][0]["status"] == "ready":
                break
            time.sleep(0.1)
        self.assertEqual(d["files"][0]["status"], "ready")
        self.assertGreater(d["files"][0]["chunks"], 1)
        self.assertEqual(d["knowledge_mode"], "full")  # small enough to go into the prompt whole
        status, d, _ = self.req("GET", f"/api/projects/{pid}/search?q=when+is+shearing", cookie=cookie)
        self.assertEqual(d["method"], "keyword")  # no embedding model here
        self.assertIn("Shearing", d["results"][0]["text"])
        # an upload the knowledge base cannot read is refused
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", f"/api/projects/{pid}/files?name=blob.bin", body=b"\x00\x01\x02" * 100,
                  headers={"Host": f"localhost:{self.port}", "Cookie": cookie, "X-CSRF-Token": csrf,
                           "Content-Type": "application/octet-stream"})
        r = c.getresponse(); r.read(); c.close()
        self.assertEqual(r.status, 400)
        # conversations in the project
        status, d, _ = self.req("POST", "/api/conversations", {"project_id": pid}, cookie=cookie, csrf=csrf)
        cid = d["conversation"]["id"]
        status, d, _ = self.req("GET", f"/api/conversations/{cid}", cookie=cookie)
        self.assertEqual(d["project"], {"id": pid, "name": "Flock"})
        status, d, _ = self.req("GET", f"/api/conversations?project={pid}", cookie=cookie)
        self.assertEqual([c["id"] for c in d["conversations"]], [cid])
        # memory
        status, d, _ = self.req("POST", "/api/memory", {"text": "Prefers metric units."}, cookie=cookie, csrf=csrf)
        mid = d["memory"]["id"]
        self.req("PATCH", f"/api/memory/{mid}", {"text": "Prefers metric units and 24-hour times."}, cookie=cookie, csrf=csrf)
        status, d, _ = self.req("GET", "/api/memory", cookie=cookie)
        self.assertEqual([m["text"] for m in d["memories"]], ["Prefers metric units and 24-hour times."])
        self.req("DELETE", f"/api/memory/{mid}", cookie=cookie, csrf=csrf)
        status, d, _ = self.req("GET", "/api/memory", cookie=cookie)
        self.assertEqual(d["memories"], [])
        # programs that run on this computer need an owner with a password, typed again
        status, d, _ = self.req("POST", "/api/connectors", {"name": "x", "transport": "stdio", "command": "/bin/true"},
                                cookie=cookie, csrf=csrf)
        self.assertEqual(status, 403)
        status, d, _ = self.req("POST", "/api/models/external", {"name": "m", "server": "/bin/true", "model": "/etc/hostname"},
                                cookie=cookie, csrf=csrf)
        self.assertEqual(status, 403)
        status, d, _ = self.req("POST", "/api/connectors", {"name": "web", "transport": "http", "url": "https://example.org/mcp"},
                                cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)  # a remote connector runs nothing here
        # deleting the project keeps its conversations
        self.req("DELETE", f"/api/projects/{pid}/files/{fid}", cookie=cookie, csrf=csrf)
        status, _, _ = self.req("DELETE", f"/api/projects/{pid}", cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        status, d, _ = self.req("GET", f"/api/conversations/{cid}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIsNone(d["conversation"]["project_id"])


    def test_sharing_between_accounts(self):
        import time
        cookie, csrf = self._owner()
        status, d, _ = self.req("POST", "/api/accounts", {"name": "lamb", "password": "pw"}, cookie=cookie, csrf=csrf)
        lamb_id = d["account"]["id"]
        status, d, r = self.req("POST", "/api/login", {"name": "lamb", "password": "pw"})
        lcookie, lcsrf = r.getheader("Set-Cookie").split(";")[0], d["csrf"]
        status, d, _ = self.req("POST", "/api/conversations", {"title": "Wool notes"}, cookie=cookie, csrf=csrf)
        cid = d["conversation"]["id"]
        self.req("POST", f"/api/conversations/{cid}/messages", {"text": "Merino wool is fine."}, cookie=cookie, csrf=csrf)
        time.sleep(0.3)  # the reply fails (no model here); the user's message stays
        status, d, _ = self.req("POST", f"/api/conversations/{cid}/share", {"to": [lamb_id]}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        shid = d["share"]["id"]
        status, d, _ = self.req("GET", "/api/shares", cookie=lcookie)
        self.assertEqual([x["id"] for x in d["shared_with_me"]], [shid])
        status, d, _ = self.req("GET", f"/api/shares/{shid}", cookie=lcookie)
        texts = [b.get("text") for m in d["share"]["snapshot"]["thread"] for b in m["blocks"]]
        self.assertIn("Merino wool is fine.", texts)
        self.assertFalse(d["share"]["mine"])
        status, d, _ = self.req("POST", f"/api/shares/{shid}/copy", {}, cookie=lcookie, csrf=lcsrf)
        copy_id = d["conversation"]["id"]
        status, d, _ = self.req("GET", f"/api/conversations/{copy_id}", cookie=lcookie)
        self.assertEqual(d["thread"][0]["blocks"][0]["text"], "Merino wool is fine.")
        status, _, _ = self.req("DELETE", f"/api/shares/{shid}", cookie=lcookie, csrf=lcsrf)
        self.assertEqual(status, 403)  # only the sharer can stop sharing
        self.req("DELETE", f"/api/shares/{shid}", cookie=cookie, csrf=csrf)
        status, _, _ = self.req("GET", f"/api/shares/{shid}", cookie=lcookie)
        self.assertEqual(status, 404)
        status, d, _ = self.req("GET", f"/api/conversations/{copy_id}", cookie=lcookie)
        self.assertEqual(status, 200)  # the copy stays


    def test_worktree(self):
        import shutil
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        cookie, csrf = self._owner()
        repo = tempfile.mkdtemp()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True, env=env)
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("baa\n")
        subprocess.run(["git", "-C", repo, "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "first"], check=True, env=env)
        status, d, _ = self.req("POST", "/api/conversations", {"title": "Fix things", "folder": repo}, cookie=cookie, csrf=csrf)
        cid = d["conversation"]["id"]
        status, d, _ = self.req("POST", f"/api/conversations/{cid}/worktree", {}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, d)
        wt = d["path"]
        self.assertTrue(os.path.exists(os.path.join(wt, "a.txt")))
        self.assertEqual(d["conversation"]["folder"], os.path.realpath(wt))
        self.assertTrue(d["branch"].startswith("baabaa/fix-things"))
        with open(os.path.join(wt, "b.txt"), "w") as f:
            f.write("new\n")
        status, d, _ = self.req("DELETE", f"/api/conversations/{cid}/worktree", cookie=cookie, csrf=csrf)
        self.assertEqual(status, 409)  # uncommitted work is not thrown away without asking
        status, d, _ = self.req("DELETE", f"/api/conversations/{cid}/worktree?force=1", cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertEqual(d["conversation"]["folder"], os.path.realpath(repo))
        self.assertFalse(os.path.exists(wt))
        branches = subprocess.run(["git", "-C", repo, "branch"], capture_output=True, text=True).stdout
        self.assertIn("baabaa/fix-things", branches)
        shutil.rmtree(repo)


@unittest.skipUnless(landlock.abi_version() >= 1, "Landlock is not available")
class TestSandbox(unittest.TestCase):
    def run_in(self, folder, cmd, net=False):
        argv = helper_argv(["/bin/sh", "-c", cmd], folder, [folder], [], net)
        return subprocess.run(argv, cwd=folder, capture_output=True, text=True, timeout=30)

    def test_confinement(self):
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as other:
            r = self.run_in(work, "echo ok > inside.txt && cat inside.txt")
            self.assertEqual(r.stdout.strip(), "ok")
            r = self.run_in(work, f"echo x > {other}/outside.txt")
            self.assertNotEqual(r.returncode, 0)
            self.assertFalse(os.path.exists(os.path.join(other, "outside.txt")))
            r = self.run_in(work, f"ls {other}")
            self.assertNotEqual(r.returncode, 0)
            py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else None  # a Python the sandbox can see
            if py:
                r = self.run_in(work, f"{py} -c \"import socket; socket.socket(socket.AF_INET)\"")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("Operation not permitted", r.stderr)


if __name__ == "__main__":
    unittest.main()
