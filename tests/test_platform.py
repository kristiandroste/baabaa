"""Who may connect (this computer only, or the local network too), and baabaa's macOS side, checked on any
machine: what macOS's ifconfig, route, ps and sysctl print, the Seatbelt profile, the launchd agent, the Apple
GPU memory rule, the macOS command checks, and the sandbox's own self-test where it can run here.
Nothing here uses the GPU or listens beyond 127.0.0.1."""

import json
import os
import plistlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from baabaa import gpu, system  # noqa: E402
from baabaa.agent import shellcheck  # noqa: E402
from baabaa.sandbox import seatbelt  # noqa: E402
from baabaa.server import lan  # noqa: E402

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\toptions=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
gif0: flags=8010<POINTOPOINT,MULTICAST> mtu 1280
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 3c:22:fb:00:00:01
\tinet6 fe80::1c5b:2a1:4f2e:9d3a%en0 prefixlen 64 secured scopeid 0xb
\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
bridge100: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.64.1 netmask 0xffffff00 broadcast 192.168.64.255
"""

ROUTE = """   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""

PS = """    1     0 /sbin/launchd
  612     1 /Applications/Ollama.app/Contents/Resources/ollama serve
  877   612 /Applications/Ollama.app/Contents/Resources/ollama runner --model /Users/a/.ollama/models/blobs/sha256-abc --port 55001
 1200   900 -zsh
"""


class TestNetworkMode(unittest.TestCase):
    def test_this_computer_only(self):
        g = lan.LanGuard(local_only=True)
        self.assertEqual(g.bind, ["127.0.0.1"])
        self.assertTrue(g.allowed_client("127.0.0.1"))
        self.assertTrue(g.allowed_client("::1"))
        self.assertTrue(g.allowed_client("local"))
        self.assertFalse(g.allowed_client("192.168.1.9"))
        self.assertFalse(g.allowed_client("172.16.4.2"))
        self.assertEqual(g.urls(8443, False), ["http://localhost:8443/"])
        self.assertTrue(g.allowed_host("localhost:8443"))

    def test_saved_mode(self):
        from baabaa.maindb import MainDB
        with tempfile.TemporaryDirectory() as d:
            db = MainDB(Path(d) / "baabaa.db")
            self.assertEqual(lan.saved_mode(db), "lan")  # data folders from before the setting
            db.set_setting("network", "local")
            self.assertEqual(lan.saved_mode(db), "local")
            db.set_setting("network", "everyone")
            self.assertEqual(lan.saved_mode(db), "lan")

    def test_server_on_this_computer_only(self):
        """A data folder set to this computer only: the server listens on 127.0.0.1 alone, over plain HTTP."""
        from fake_ollama import FakeOllama
        from baabaa.maindb import MainDB
        ollama = FakeOllama()
        url = ollama.start()
        with tempfile.TemporaryDirectory() as d:
            MainDB(Path(d) / "baabaa.db").set_setting("network", "local")
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            env = {k: v for k, v in os.environ.items() if k not in ("BAABAA_HOME", "PYTHONPATH")}
            env["BAABAA_PYTHON"] = sys.executable
            run = lambda *a: subprocess.run([str(ROOT / "bin" / "baabaa"), "--data", d, *a], env=env, cwd="/",  # noqa: E731
                                            capture_output=True, text=True, timeout=120)
            try:
                r = run("start", "--port", str(port), "--ollama", url)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(f"running on this computer only: http://localhost:{port}/", r.stdout)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as resp:
                    self.assertEqual(json.load(resp)["name"], "baabaa")
                ifaces, default = lan.interfaces(), lan.default_interface()
                lan_ip = next((i["ip"] for i in ifaces if i["name"] == default), None)
                if lan_ip:
                    with self.assertRaises(OSError):
                        socket.create_connection((lan_ip, port), timeout=2).close()
                self.assertIn("accepts this computer only", run("status").stdout)
                self.assertIn("this computer only", run("network").stdout)
            finally:
                run("stop")
                ollama.stop()

    def test_network_command(self):
        with tempfile.TemporaryDirectory() as d:
            env = {k: v for k, v in os.environ.items() if k not in ("BAABAA_HOME", "PYTHONPATH")}
            env.update(BAABAA_PYTHON=sys.executable, BAABAA_HOME=d)
            run = lambda *a: subprocess.run([str(ROOT / "bin" / "baabaa"), *a], env=env, cwd="/",  # noqa: E731
                                            capture_output=True, text=True, timeout=60)
            self.assertEqual(run("account", "add", "shepherd").returncode, 0)
            self.assertIn("this computer and other devices", run("network").stdout)
            r = run("network", "lan")
            self.assertIn("anyone on your network could open them: shepherd", r.stdout)
            r = run("network", "local")
            self.assertIn("now accepts connections from this computer only", r.stdout)
            self.assertNotEqual(run("network", "everyone").returncode, 0)


class TestMacParsing(unittest.TestCase):
    def test_ifconfig_and_route(self):
        ifaces = lan.parse_ifconfig(IFCONFIG)
        by = {i["name"]: i for i in ifaces}
        self.assertEqual(by["en0"]["ip"], "192.168.1.20")
        self.assertEqual(by["en0"]["netmask"], "255.255.255.0")
        self.assertEqual(by["en0"]["network"], "192.168.1.0/24")
        self.assertEqual(by["lo0"]["network"], "127.0.0.0/8")
        self.assertNotIn("gif0", by)
        self.assertEqual(lan.parse_route_get(ROUTE), "en0")
        self.assertIsNone(lan.parse_route_get("route: writing to routing socket: not in table\n"))

    def test_ps_and_cpu_time(self):
        procs = system.parse_ps(PS)
        self.assertEqual([p["pid"] for p in procs], [1, 612, 877, 1200])
        self.assertEqual(procs[1]["argv"][:2], [b"/Applications/Ollama.app/Contents/Resources/ollama", b"serve"])
        self.assertEqual(procs[2]["ppid"], 612)
        self.assertEqual(system.parse_cputime("0:01.50"), 1.5)
        self.assertEqual(system.parse_cputime("1:02:03.25"), 3723.25)
        self.assertEqual(system.parse_cputime("2-01:00:00"), 176400)
        self.assertIsNone(system.parse_cputime("soon"))

    def test_ollama_runners_found_from_ps(self):
        from baabaa import cpuwatch
        with mock.patch.object(cpuwatch, "processes", return_value=system.parse_ps(PS)):
            self.assertEqual(cpuwatch.ollama_runner_pids(), [877])
            self.assertEqual(cpuwatch.ollama_runner_pids("/Users/a/.ollama/models/blobs/sha256-abc"), [877])

    def test_apple_gpu_memory(self):
        g = 2**30
        self.assertEqual(gpu.apple_gpu_memory(16 * g), 16 * g * 2 // 3)
        self.assertEqual(gpu.apple_gpu_memory(64 * g), 48 * g)
        self.assertEqual(gpu.apple_gpu_memory(16 * g, wired_limit_mb=12288), 12 * g)

    def test_apple_gpu(self):
        values = {"machdep.cpu.brand_string": "Apple M2 Pro", "hw.memsize": str(32 * 2**30), "iogpu.wired_limit_mb": "0"}
        with mock.patch.object(gpu, "MAC", True), mock.patch.object(gpu, "sysctl", values.get), \
                mock.patch.object(gpu.platform, "machine", return_value="arm64"):
            g = gpu.GPU()
            self.assertTrue(g.available)
            self.assertFalse(g.readings)
            self.assertEqual(g.name, "Apple M2 Pro GPU")
            self.assertEqual(g.memory()["total"], 32 * 2**30 * 2 // 3)
            self.assertIsNone(g.processes())
            self.assertIsNone(g.energy_mj())
            self.assertEqual(g.sample()["vram_total"], 32 * 2**30 * 2 // 3)
        with mock.patch.object(gpu, "MAC", True), mock.patch.object(gpu.platform, "machine", return_value="x86_64"):
            g = gpu.GPU()
            self.assertFalse(g.available)
            self.assertIn("Intel Mac", g.error)


class TestSeatbelt(unittest.TestCase):
    def test_profile(self):
        text, params = seatbelt.profile(["/Users/a/proj", "/Users/a/.local/share/baabaa/accounts/x/home"], ["/Users/a/ref"],
                                        False, resolve=lambda p: p)
        lines = text.splitlines()
        self.assertEqual(lines[:2], ["(version 1)", "(allow default)"])
        # areas closed first, then the given folders opened: the last matching rule wins
        closed = next(i for i, l in enumerate(lines) if l.startswith("(deny file-read-data"))
        writes = lines.index("(deny file-write*)")
        opened = next(i for i, l in enumerate(lines) if l.startswith("(allow file-read-data file-read-xattr file-write*"))
        readonly = next(i for i, l in enumerate(lines) if l.startswith("(allow file-read-data file-read-xattr (subpath"))
        self.assertLess(closed, opened)
        self.assertLess(writes, opened)
        self.assertLess(opened, readonly)
        self.assertIn("(deny network*)", lines)
        self.assertFalse([l for l in lines if l.startswith("(allow network")])
        self.assertEqual([params[k] for k in ("P0", "P1", "P2", "P3")], list(seatbelt.CLOSED))
        self.assertEqual(params["P4"], "/Users/a/proj")
        self.assertEqual(params["P6"], "/Users/a/ref")
        text, _ = seatbelt.profile(["/w"], [], True, ports=(80, 443), resolve=lambda p: p)
        self.assertIn('(allow network-outbound (remote tcp "*:80") (remote tcp "*:443"))', text)
        self.assertIn("mDNSResponder", text)

    def test_paths_are_parameters(self):
        odd = '/Users/a/My "Project" (2)'
        text, params = seatbelt.profile([odd], [], False, resolve=lambda p: p)
        self.assertNotIn("Project", text)
        self.assertIn(odd, params.values())
        argv = seatbelt.argv(["/bin/bash", "-c", "ls"], [odd], [], False)
        self.assertEqual(argv[0], "/usr/bin/sandbox-exec")
        self.assertEqual(argv[1], "-p")
        self.assertEqual(argv[-3:], ["/bin/bash", "-c", "ls"])
        self.assertIn("-D", argv)

    def test_sandbox_path_on_mac(self):
        from baabaa import sandbox
        with mock.patch.object(sandbox, "MAC", True), \
                mock.patch.dict(os.environ, {"PATH": "/Users/a/.pyenv/shims:/opt/homebrew/bin:/usr/bin:/Users/a/proj/bin"}):
            path = sandbox.sandbox_path(["/Users/a/proj"]).split(":")
        self.assertIn("/opt/homebrew/bin", path)
        self.assertIn("/usr/bin", path)
        self.assertIn("/Users/a/proj/bin", path)  # inside a folder the command may read
        self.assertNotIn("/Users/a/.pyenv/shims", path)


class TestLaunchd(unittest.TestCase):
    def test_agent(self):
        from baabaa import installer
        agent = plistlib.loads(installer.plist_bytes("/x/bin/baabaa", ["--port", "9000"], None, "/d/logs/server.log"))
        self.assertEqual(agent["Label"], installer.LABEL)
        self.assertEqual(agent["ProgramArguments"], ["/x/bin/baabaa", "serve", "--port", "9000"])
        self.assertTrue(agent["RunAtLoad"])
        self.assertEqual(agent["KeepAlive"], {"SuccessfulExit": False})
        self.assertNotIn("EnvironmentVariables", agent)
        agent = plistlib.loads(installer.plist_bytes("/x/bin/baabaa", [], "/data", "/data/logs/server.log"))
        self.assertEqual(agent["EnvironmentVariables"], {"BAABAA_HOME": "/data"})


class TestMacCommands(unittest.TestCase):
    def test_commands_and_case(self):
        F = "/Users/u/proj"
        for cmd in ("launchctl load x.plist", "osascript -e 'beep'", "security dump-keychain", "diskutil eraseDisk x y z"):
            self.assertEqual(shellcheck.classify(cmd, F).kind, "dangerous", cmd)
        self.assertEqual(shellcheck.classify("cp x ~/Library/LaunchAgents/a.plist", F).kind, "dangerous")
        with mock.patch.object(shellcheck.sys, "platform", "darwin"):
            self.assertTrue(shellcheck._sensitive("/Users/u/.SSH/id_ed25519"))  # macOS disks ignore case
        with mock.patch.object(shellcheck.sys, "platform", "linux"):
            self.assertFalse(shellcheck._sensitive("/home/u/.SSH-notes/x"))


class TestShellScripts(unittest.TestCase):
    def test_plain_ascii(self):
        # macOS's sh takes a byte above 127 that follows $NAME as part of the name, and `set -u` then stops the
        # script: an ellipsis after $VERSION did that to the installer
        for name in ("install.sh", "bin/baabaa"):
            self.assertTrue((ROOT / name).read_bytes().isascii(), f"{name} has characters outside ASCII")


class TestAsMac(unittest.TestCase):
    def test_every_module_imports_as_macos(self):
        """baabaa's macOS branches, run here: sys.platform says darwin before baabaa is imported (the standard
        library is loaded first, so only baabaa's own branches change)."""
        code = """
import sys, json
import asyncio, ctypes.util, mimetypes, plistlib, shlex, socket, ssl, subprocess, tarfile, urllib.request, uuid
sys.platform = "darwin"
import importlib, pkgutil, baabaa
failed = []
for m in pkgutil.walk_packages(baabaa.__path__, "baabaa."):
    try:
        importlib.import_module(m.name)
    except Exception as exc:
        failed.append(m.name + ": " + repr(exc))
from baabaa import sandbox, system
from baabaa.sandbox import landlock
from baabaa.server import lan
print(json.dumps({"failed": failed, "mac": system.MAC, "abi": landlock.abi_version(),
                  "local": lan.LanGuard(local_only=True).urls(1, False),
                  "lc": sandbox.sandbox_env("/h", "/t")["LC_ALL"]}))
"""
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONPATH"] = str(ROOT)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env, cwd="/")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["failed"], [])
        self.assertTrue(out["mac"])
        self.assertEqual(out["abi"], 0)  # no Linux system call is ever made elsewhere
        self.assertEqual(out["local"], ["http://localhost:1/"])
        self.assertEqual(out["lc"], "en_US.UTF-8")


@unittest.skipUnless(sys.platform == "linux", "Landlock is Linux's")
class TestSandboxSelfCheck(unittest.TestCase):
    def test_every_promise_holds(self):
        from baabaa.sandbox import check, landlock
        if landlock.abi_version() < 4:
            self.skipTest("Landlock ABI 4 (ports) is needed for every check")
        with tempfile.TemporaryDirectory() as d:
            results = check.run_checks(Path(d))
        failed = [(n, detail) for n, ok, detail in results if not ok]
        self.assertEqual(failed, [])
        self.assertGreaterEqual(len(results), 12)


if __name__ == "__main__":
    unittest.main()
