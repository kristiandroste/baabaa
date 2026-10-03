"""`baabaa start | stop | restart | status` and the one-server-per-data-folder lock, with the real server
on loopback in plain HTTP. Ollama is a stand-in; nothing here uses the GPU."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from fake_ollama import FakeOllama  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.ollama = FakeOllama()
        self.ollama_url = self.ollama.start()
        self.port = free_port()

    def tearDown(self):
        self.baabaa("stop")
        self.ollama.stop()
        self.tmp.cleanup()

    def baabaa(self, *args, cwd="/"):
        """The command as a person types it: the launcher, from another folder."""
        env = {k: v for k, v in os.environ.items() if k not in ("BAABAA_HOME", "PYTHONPATH")}
        env["BAABAA_PYTHON"] = sys.executable
        return subprocess.run([str(ROOT / "bin" / "baabaa"), "--data", self.data, *args], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=120)

    def start(self):
        return self.baabaa("start", "--http", "--bind", "127.0.0.1", "--port", str(self.port), "--ollama", self.ollama_url)

    def server_pid(self) -> int:
        return int(Path(self.data, "server.lock").read_text())

    def test_start_status_stop(self):
        r = self.baabaa("status")
        self.assertEqual(r.returncode, 3)
        self.assertIn("not running", r.stdout)
        r = self.start()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"http://localhost:{self.port}/", r.stdout)
        self.assertIn("First run: create the owner account", r.stdout)
        pid = self.server_pid()
        # it runs in a session of its own, with no terminal: closing the one it came from cannot stop it
        self.assertEqual(os.getsid(pid), pid)
        if sys.platform == "linux":
            with open(f"/proc/{pid}/stat") as f:
                self.assertEqual(f.read().rsplit(")", 1)[1].split()[4], "0")  # tty_nr: none
        r = self.baabaa("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn(f"running in the background (pid {pid}", r.stdout)
        self.assertIn("GPU: idle", r.stdout)
        self.assertIn("No accounts yet", r.stdout)
        # a second start, or a second server on the same data folder, does not start another
        r = self.start()
        self.assertIn(f"already running (pid {pid})", r.stdout)
        r = self.baabaa("serve", "--http", "--bind", "127.0.0.1", "--port", str(free_port()), "--ollama", self.ollama_url)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already running", r.stdout)
        # restart keeps the options
        r = self.baabaa("restart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotEqual(self.server_pid(), pid)
        self.assertIn(f"http://localhost:{self.port}/", r.stdout)
        r = self.baabaa("stop")
        self.assertIn("baabaa stopped", r.stdout)
        self.assertEqual(self.baabaa("status").returncode, 3)
        self.assertFalse(Path(self.data, "server.json").exists())
        self.assertIn("baabaa stopped.", Path(self.data, "logs", "server.log").read_text())

    def test_ended_on_its_own(self):
        self.assertEqual(self.start().returncode, 0)
        os.kill(self.server_pid(), signal.SIGKILL)
        time.sleep(0.5)
        r = self.baabaa("status")
        self.assertEqual(r.returncode, 3)
        self.assertIn("ended on its own", r.stdout)
        self.assertEqual(self.start().returncode, 0)  # and it starts again: the lock went with the process

    def test_start_fails_cleanly(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", self.port))
            busy.listen()
            r = self.start()
        self.assertEqual(r.returncode, 1)
        self.assertIn("did not start", r.stdout)
        self.assertEqual(self.baabaa("status").returncode, 3)
        state = Path(self.data, "server.json")
        self.assertFalse(state.exists(), state.read_text() if state.exists() else "")


if __name__ == "__main__":
    unittest.main()
