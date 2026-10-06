"""Installing, updating and restarting: the release signature (RFC 8032's vectors), the release list and its
channels, downloads and unpacking (refusing anything unexpected), versions side by side, the switch, database
copies, the installer script and its self-contained form, the update API and its permissions, and a real server
that restarts in place into a new version, and back again when that version does not start.

Releases come from a folder feed signed with a test key; nothing here uses the network or the GPU."""

import asyncio
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
sys.path.insert(0, str(ROOT / "tools"))

from baabaa import signing, update  # noqa: E402
from fake_ollama import FakeOllama  # noqa: E402


# a signer for the tests (RFC 8032 section 5.1.6); baabaa itself only verifies ---------------------------------------
def _compress(p) -> bytes:
    zi = pow(p[2], signing._P - 2, signing._P)
    x, y = p[0] * zi % signing._P, p[1] * zi % signing._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def keypair(seed: bytes):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a = (a & ((1 << 254) - 8)) | (1 << 254)
    return (a, h[32:]), _compress(signing._mul(a, signing._G))


def sign(secret, public: bytes, msg: bytes) -> bytes:
    a, prefix = secret
    r = int.from_bytes(hashlib.sha512(prefix + msg).digest(), "little") % signing._L
    big_r = _compress(signing._mul(r, signing._G))
    k = int.from_bytes(hashlib.sha512(big_r + public + msg).digest(), "little") % signing._L
    return big_r + ((r + k * a) % signing._L).to_bytes(32, "little")


SECRET, PUBLIC = keypair(b"baabaa test key".ljust(32, b"."))
KID = signing.key_id(PUBLIC.hex())
TEST_KEYS = {KID: PUBLIC.hex()}


def make_release(where: Path, version: str, broken: bool = False, date: str = "2026-01-01", stable: bool = True) -> dict:
    """A release archive of this checkout's release files, as `version`, trusting the test key. `broken` makes a
    version that imports fine (so it passes its start-up check) but fails while the server starts."""
    work = where / f"src-{version}" / f"baabaa-{version}"
    shutil.rmtree(work.parent, ignore_errors=True)
    for name in update.RELEASE_FILES:
        p = ROOT / name
        if p.is_dir():
            shutil.copytree(p, work / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif p.is_file():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, work / name)
    init = work / "baabaa" / "__init__.py"
    init.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', init.read_text()))
    log = work / "CHANGELOG.md"
    log.write_text(f"# Changelog\n\n## {version} - {date}\n\n- What changed in {version}\n\n" + log.read_text().split("\n", 1)[1])
    sig = work / "baabaa" / "signing.py"
    sig.write_text(re.sub(r"RELEASE_KEYS = \{[^}]*\}", f"RELEASE_KEYS = {{{KID!r}: {PUBLIC.hex()!r}}}", sig.read_text()))
    if broken:
        app = work / "baabaa" / "app.py"
        app.write_text(app.read_text().replace("    async def startup(self) -> None:\n",
                                               "    async def startup(self) -> None:\n        raise RuntimeError('broken on purpose')\n", 1))
    tarball = where / f"baabaa-{version}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(work, arcname=f"baabaa-{version}")
    data = tarball.read_bytes()
    return {"version": version, "date": date, "file": tarball.name, "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "python": "3.10", "notes": [f"What changed in {version}"], "stable": stable}


def publish(feed: Path, releases: list[dict], key=(SECRET, PUBLIC)) -> bytes:
    data = (json.dumps({"format": 1, "releases": releases}) + "\n").encode()
    (feed / "releases.json").write_bytes(data)
    (feed / "releases.json.sig").write_text(f"{signing.key_id(key[1].hex())} {sign(key[0], key[1], data).hex()}\n")
    return data


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------------------------------------------------
class TestSignature(unittest.TestCase):
    def test_rfc8032_vectors(self):
        vectors = json.loads((ROOT / "tests" / "fixtures" / "ed25519_vectors.json").read_text())["vectors"]
        self.assertEqual(len(vectors), 5)
        for v in vectors:
            pub, msg, sig = (bytes.fromhex(v[k]) for k in ("public", "message", "signature"))
            with self.subTest(test=v["name"]):
                self.assertTrue(signing.verify(pub, msg, sig))
                self.assertFalse(signing.verify(pub, msg + b"\0", sig))
                for i in (0, 31, 32, 63):
                    bad = bytearray(sig)
                    bad[i] ^= 0x04
                    self.assertFalse(signing.verify(pub, msg, bytes(bad)))
                # the test signer gives the RFC's signature: both sides agree with the RFC
                secret, public = keypair(bytes.fromhex(v["secret"]))
                self.assertEqual(public, pub)
                self.assertEqual(sign(secret, public, msg), sig)

    def test_rejects_malformed(self):
        msg = b"hello"
        sig = sign(SECRET, PUBLIC, msg)
        self.assertTrue(signing.verify(PUBLIC, msg, sig))
        self.assertFalse(signing.verify(PUBLIC[:31], msg, sig))
        self.assertFalse(signing.verify(PUBLIC, msg, sig[:63]))
        too_big_s = sig[:32] + (int.from_bytes(sig[32:], "little") + signing._L).to_bytes(32, "little")
        self.assertFalse(signing.verify(PUBLIC, msg, too_big_s))  # s must be below L (no malleability)
        self.assertEqual(signing.verify_release_file(msg, f"{KID} {sig.hex()}", TEST_KEYS), KID)
        self.assertIsNone(signing.verify_release_file(msg, f"{KID} {sig.hex()}"))  # not a release key
        self.assertIsNone(signing.verify_release_file(msg, "nonsense", TEST_KEYS))
        self.assertIsNone(signing.verify_release_file(msg, f"{KID} zz", TEST_KEYS))

    def test_release_key_is_real(self):
        self.assertTrue(signing.RELEASE_KEYS)
        for kid, pub in signing.RELEASE_KEYS.items():
            self.assertEqual(kid, signing.key_id(pub))
            self.assertIsNotNone(signing._decompress(bytes.fromhex(pub)))


class TestVersions(unittest.TestCase):
    def test_order(self):
        order = ["0.9.0", "0.9.1", "0.10.0", "1.0.0-rc.1", "1.0.0", "1.0.1", "10.0.0"]
        self.assertEqual(sorted(reversed(order), key=update.version_key), order)
        self.assertTrue(update.newer("1.0.0", "0.9.9"))
        self.assertTrue(update.newer("1.0.0", None))
        self.assertFalse(update.newer("1.0.0", "1.0.0"))
        self.assertFalse(update.newer(None, "1.0.0"))
        self.assertFalse(update.valid_version("1.0"))

    def test_channels(self):
        day = 86400
        now = time.mktime(time.strptime("2026-10-20", "%Y-%m-%d"))
        index = {"releases": [
            {"version": "1.0.0", "date": "2026-10-01", "stable": False},
            {"version": "1.1.0", "date": "2026-10-10", "stable": False},
            {"version": "1.2.0", "date": "2026-10-18", "stable": False},
            {"version": "1.3.0", "date": "2026-10-19", "pulled": True},
            {"version": "1.2.1", "date": "2026-10-19", "stable": True},
            {"version": "2.0.0", "date": "2026-10-19", "python": "3.99"},
        ]}
        self.assertEqual(update.pick(index, "latest", now)["version"], "1.2.1")  # 1.3.0 pulled, 2.0.0 needs a newer Python
        self.assertEqual(update.pick(index, "stable", now)["version"], "1.2.1")  # marked stable at once
        index["releases"][4]["stable"] = False
        self.assertEqual(update.pick(index, "stable", now)["version"], "1.1.0")  # 10 days out; 1.2.0 only 2
        self.assertEqual(update.pick(index, "stable", now + 5.5 * day)["version"], "1.2.0")
        self.assertIsNone(update.pick({"releases": []}, "latest", now))

    def test_changelog(self):
        notes = update.changelog(ROOT)
        self.assertTrue(notes)
        self.assertTrue(all(isinstance(n, str) and n for n in notes))


# a folder feed and a private PREFIX --------------------------------------------------------------------------------
class FeedCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.feed = self.dir / "feed"
        self.feed.mkdir()
        self.env = mock.patch.dict(os.environ, {"BAABAA_PREFIX": str(self.dir / "prefix"), "BAABAA_BIN_DIR": str(self.dir / "bin"),
                                                "BAABAA_UPDATE_URL": self.feed.as_uri()})
        self.env.start()
        os.environ.pop("BAABAA_DISABLE_UPDATES", None)
        self.keys = mock.patch.dict(signing.RELEASE_KEYS, TEST_KEYS, clear=True)
        self.keys.start()

    def tearDown(self):
        self.keys.stop()
        self.env.stop()
        self.tmp.cleanup()

    def release(self, version, **kw):
        return make_release(self.feed, version, **kw)


class TestReleaseList(FeedCase):
    def test_signature_and_format(self):
        r = self.release("9.9.1")
        publish(self.feed, [r])
        index = update.fetch_releases()
        self.assertEqual([x["version"] for x in index["releases"]], ["9.9.1"])
        # anything changed after signing is refused
        data = (self.feed / "releases.json").read_bytes()
        (self.feed / "releases.json").write_bytes(data.replace(b"9.9.1", b"9.9.9"))
        with self.assertRaisesRegex(update.UpdateError, "signature"):
            update.fetch_releases()
        # signed by a key baabaa does not know
        other = keypair(b"someone else".ljust(32, b"."))
        publish(self.feed, [r], key=other)
        with self.assertRaisesRegex(update.UpdateError, "signature"):
            update.fetch_releases()
        # no signature at all
        publish(self.feed, [r])
        (self.feed / "releases.json.sig").unlink()
        with self.assertRaisesRegex(update.UpdateError, "no signature"):
            update.fetch_releases()
        # nothing published yet
        (self.feed / "releases.json").unlink()
        with self.assertRaises(update.NoReleases):
            update.fetch_releases()
        # a format this version does not know
        data = b'{"format": 2, "releases": []}\n'
        (self.feed / "releases.json").write_bytes(data)
        (self.feed / "releases.json.sig").write_text(f"{KID} {sign(SECRET, PUBLIC, data).hex()}\n")
        with self.assertRaisesRegex(update.UpdateError, "format"):
            update.fetch_releases()

    def test_stage_switch_and_back(self):
        r1, r2 = self.release("9.9.1"), self.release("9.9.2")
        publish(self.feed, [r1, r2])
        index = update.fetch_releases()
        update.stage(index, r1)
        self.assertIsNone(update.switch("9.9.1"))
        self.assertEqual(update.installed_version(), "9.9.1")
        link, how = update.ensure_launcher()
        self.assertEqual(how, "made")
        self.assertEqual(Path(os.readlink(link)), update.prefix() / "current" / "bin" / "baabaa")
        out = subprocess.run([str(link), "--version"], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "BAABAA_PYTHON": sys.executable})
        self.assertEqual(out.stdout.strip(), "baabaa 9.9.1")
        update.stage(index, r2)
        self.assertEqual(update.switch("9.9.2"), "9.9.1")
        self.assertEqual(update.load_state()["previous"], "9.9.1")
        out = subprocess.run([str(link), "--version"], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "BAABAA_PYTHON": sys.executable})
        self.assertEqual(out.stdout.strip(), "baabaa 9.9.2")
        update.switch("9.9.1")  # going back is the same switch
        self.assertEqual(update.installed_version(), "9.9.1")
        # a launcher someone made themselves is left alone
        link.unlink()
        link.write_text("#!/bin/sh\n")
        self.assertEqual(update.ensure_launcher()[1], "custom")

    def test_download_checks(self):
        r = self.release("9.9.1")
        bad = dict(r, sha256="0" * 64)
        publish(self.feed, [bad])
        with self.assertRaisesRegex(update.UpdateError, "SHA-256"):
            update.stage(update.fetch_releases(), bad)
        self.assertFalse((update.prefix() / "versions" / "9.9.1").exists())
        short = dict(r, size=r["size"] - 10)
        publish(self.feed, [short])
        with self.assertRaisesRegex(update.UpdateError, "larger than expected"):
            update.stage(update.fetch_releases(), short)

    def test_unpack_refuses_unexpected_entries(self):
        def archive(name, member):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                info = tarfile.TarInfo(member["name"])
                if member.get("link"):
                    info.type, info.linkname = tarfile.SYMTYPE, member["link"]
                    tar.addfile(info)
                else:
                    info.size = 3
                    tar.addfile(info, io.BytesIO(b"bad"))
            p = self.dir / name
            p.write_bytes(buf.getvalue())
            return p
        for i, member in enumerate([{"name": "baabaa-9.9.1/../../escape"}, {"name": "/etc/evil"},
                                    {"name": "other/file"}, {"name": "baabaa-9.9.1/link", "link": "/etc/passwd"}]):
            with self.subTest(member=member):
                with self.assertRaises(update.UpdateError):
                    update.unpack(archive(f"bad{i}.tar.gz", member), "9.9.1")
                self.assertFalse((update.prefix() / "versions" / "9.9.1").exists())
        self.assertFalse((self.dir / "escape").exists())

    def test_check_downloads_ahead_and_status(self):
        r1, r2 = self.release("9.9.1"), self.release("9.9.2")
        publish(self.feed, [r1])
        update.stage(update.fetch_releases(), r1)
        update.switch("9.9.1")
        with mock.patch.object(update, "kind", return_value="installed"):
            st = update.check()
            self.assertIsNone(st["error"])
            self.assertEqual(update.status("9.9.1")["state"], "up_to_date")
            publish(self.feed, [r1, r2])
            st = update.check(download_too=False)
            self.assertEqual(update.status("9.9.1")["state"], "available")
            st = update.check()
            self.assertEqual(st["staged"], "9.9.2")
            s = update.status("9.9.1")
            self.assertEqual((s["state"], s["target"]), ("ready", "9.9.2"))
            self.assertEqual(s["latest"]["notes"], ["What changed in 9.9.2"])
            update.switch("9.9.2")
            s = update.status("9.9.1")  # installed, but this server still runs 9.9.1
            self.assertEqual((s["state"], s["target"]), ("installed", "9.9.2"))
            self.assertEqual(update.status("9.9.2")["state"], "up_to_date")
            self.assertEqual(update.status("9.9.2")["previous"], "9.9.1")
            # turned off on this computer
            with mock.patch.dict(os.environ, {"BAABAA_DISABLE_UPDATES": "1"}):
                self.assertIn("turned off", update.check()["error"])
        # a checkout is never updated in place, only told its files changed
        self.assertEqual(update.status("9.9.2", changed=True)["state"], "changed")

    def test_cleanup_keeps_what_matters(self):
        index_releases = [self.release(v) for v in ("9.9.1", "9.9.2", "9.9.3", "9.9.4", "9.9.5")]
        publish(self.feed, index_releases)
        index = update.fetch_releases()
        for r in index_releases:
            update.stage(index, r)
        update.switch("9.9.4")
        update.switch("9.9.5")
        gone = update.cleanup({"9.9.1"})  # a running server uses 9.9.1
        self.assertEqual(gone, ["9.9.2"])
        self.assertEqual(update.versions_on_disk(), ["9.9.1", "9.9.3", "9.9.4", "9.9.5"])

    def test_backups(self):
        data = self.dir / "data"
        (data / "accounts" / "a1").mkdir(parents=True)
        for p in (data / "baabaa.db", data / "stats.db", data / "accounts" / "a1" / "account.db"):
            con = sqlite3.connect(str(p))
            con.execute("CREATE TABLE t (x)")
            con.execute("INSERT INTO t VALUES (?)", (p.name,))
            con.commit()
            con.close()
        made = [update.backup_databases(data, "9.9.1") for _ in range(3)]
        for m in made:
            time.sleep(0.01)
        self.assertTrue((made[-1] / "accounts" / "a1" / "account.db").exists())
        con = sqlite3.connect(str(made[-1] / "baabaa.db"))
        self.assertEqual(con.execute("SELECT x FROM t").fetchone()[0], "baabaa.db")
        con.close()
        self.assertLessEqual(len(list((data / "backups").iterdir())), update.KEEP_BACKUPS)

    def test_release_tool(self):
        import release
        self.assertEqual(release.tarball("9.9.9", 1700000000), release.tarball("9.9.9", 1700000000))
        names = tarfile.open(fileobj=io.BytesIO(release.tarball("9.9.9", 1700000000))).getnames()
        self.assertIn("baabaa-9.9.9/bin/baabaa", names)
        self.assertIn("baabaa-9.9.9/install.sh", names)
        self.assertFalse([n for n in names if "__pycache__" in n or "/tests/" in n or n.endswith((".pyc", "INVENTORY.md"))])
        words = self.dir / "words.txt"
        words.write_text("# a comment\nno-such-word-anywhere-[0-9]{9}\n")
        with mock.patch.object(release, "PRIVATE_WORDS", words):
            self.assertEqual(release.private_scan(), [])
            words.write_text("baabaa\n")
            self.assertTrue(release.private_scan())
        # a word that a later commit removed is still in the history
        repo = self.dir / "history"
        repo.mkdir()
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], cwd=repo,
                                        check=True, capture_output=True)
        git("init", "-q")
        (repo / "notes.txt").write_text("the secretword is here\n")
        git("add", "-A"), git("commit", "-q", "-m", "first")
        (repo / "notes.txt").write_text("nothing here\n")
        git("add", "-A"), git("commit", "-q", "-m", "second")
        self.assertEqual(len(release.history_scan([re.compile("secretword", re.I)], repo)), 2)  # added, then removed
        self.assertEqual(release.history_scan([re.compile("no-such-word", re.I)], repo), [])


# the installer script, as a person runs it ---------------------------------------------------------------------------
class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.home = self.dir / "home"
        self.home.mkdir()
        self.feed = self.dir / "feed"
        self.feed.mkdir()
        self.ollama = FakeOllama()
        self.ollama_url = self.ollama.start()

    def tearDown(self):
        if (self.home / ".local" / "bin" / "baabaa").exists():
            self.run_cmd([str(self.home / ".local" / "bin" / "baabaa"), "stop"], check=False)
        self.ollama.stop()
        self.tmp.cleanup()

    def environ(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BAABAA_", "XDG_")) and k not in ("PYTHONPATH",)}
        env.update(HOME=str(self.home), BAABAA_UPDATE_URL=self.feed.as_uri(), BAABAA_PYTHON=sys.executable,
                   BAABAA_ASSUME="no", BAABAA_OLLAMA_URL=self.ollama_url, TMPDIR=str(self.dir))
        return env

    def run_cmd(self, argv, check=True, timeout=180):
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=self.environ(), cwd=str(self.dir))
        if check:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def baabaa(self, *args, check=True):
        return self.run_cmd([str(self.home / ".local" / "bin" / "baabaa"), *args], check=check)

    def health(self, port):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            c.request("GET", "/api/health", headers={"Host": f"localhost:{port}"})
            r = c.getresponse()
            return json.loads(r.read()) if r.status == 200 else None
        except OSError:
            return None

    def wait_version(self, port, version, seconds=90):
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            h = self.health(port)
            if h and h.get("version") == version:
                return h
            time.sleep(0.3)
        self.fail(f"the server did not come back as {version}: {self.health(port)}")

    def test_install_update_restart_fallback_and_remove(self):
        publish(self.feed, [make_release(self.feed, "9.9.1")])
        r = self.run_cmd(["sh", str(ROOT / "install.sh")])
        self.assertIn("baabaa 9.9.1 is installed", r.stdout)
        self.assertIn("Start it later with: baabaa start", r.stdout)
        # a new data folder: asked who may connect; no means this computer only
        self.assertIn("baabaa accepts this computer only", r.stdout)
        con = sqlite3.connect(str(self.home / ".local" / "share" / "baabaa" / "baabaa.db"))
        self.assertEqual(con.execute("SELECT value FROM settings WHERE key='network'").fetchone()[0], '"local"')
        con.close()
        prefix = self.home / ".local" / "lib" / "baabaa"
        self.assertEqual(os.readlink(prefix / "current"), "versions/9.9.1")
        self.assertEqual(self.baabaa("--version").stdout.strip(), "baabaa 9.9.1")
        self.assertFalse((prefix / "versions" / "9.9.1" / "tests").exists())

        port = free_port()
        self.baabaa("start", "--http", "--bind", "127.0.0.1", "--port", str(port), "--ollama", self.ollama_url)
        pid = self.wait_version(port, "9.9.1")
        data = self.home / ".local" / "share" / "baabaa"
        pid = int((data / "server.lock").read_text())

        # a new release: `baabaa update --now` installs it and the server restarts in place (same process)
        publish(self.feed, [make_release(self.feed, "9.9.1"), make_release(self.feed, "9.9.2")])
        r = self.baabaa("update", "--now")
        self.assertIn("Updated baabaa from 9.9.1 to 9.9.2", r.stdout)
        self.assertIn("What changed in 9.9.2", r.stdout)
        self.wait_version(port, "9.9.2")
        self.assertEqual(int((data / "server.lock").read_text()), pid)
        self.assertTrue(list((data / "backups").iterdir()))  # the databases, copied before the switch
        self.assertIn("baabaa 9.9.2 is running", self.baabaa("status").stdout)

        # a release that does not start: the server goes back to the version it came from by itself
        publish(self.feed, [make_release(self.feed, v, broken=(v == "9.9.3")) for v in ("9.9.1", "9.9.2", "9.9.3")])
        self.baabaa("update", "--now")
        time.sleep(2)
        self.wait_version(port, "9.9.2")
        self.assertEqual(os.readlink(prefix / "current"), "versions/9.9.2")
        state = json.loads((prefix / "update.json").read_text())
        self.assertEqual(state["failed"], "9.9.3")
        self.assertIn("did not start", state["error"])
        self.assertEqual(int((data / "server.lock").read_text()), pid)
        log = (data / "logs" / "server.log").read_text()
        self.assertIn("going back to 9.9.2", log)
        # and it is not offered again
        self.assertIn("up to date", self.baabaa("update", "--check").stdout)

        # the self-contained installer updates in place too, without the feed
        import release
        tarball = (self.feed / "baabaa-9.9.4.tar.gz")
        make_release(self.feed, "9.9.4")
        with mock.patch.object(release, "ROOT", ROOT):
            script = release.bundle_script("9.9.4", tarball.read_bytes())
        bundle = self.dir / "baabaa-9.9.4-install.sh"
        bundle.write_bytes(script)
        shutil.rmtree(self.feed)
        r = self.run_cmd(["sh", str(bundle)])
        self.assertIn("Installing baabaa 9.9.4 from this file", r.stdout)
        self.wait_version(port, "9.9.4")

        # remove: the program goes, the data stays; --purge removes the data and never touches Ollama
        r = self.baabaa("uninstall", "--yes")
        self.assertFalse(prefix.exists())
        self.assertFalse((self.home / ".local" / "bin" / "baabaa").exists())
        self.assertTrue((data / "baabaa.db").exists())
        self.assertIsNone(self.health(port))
        tags = self.ollama.tags() if hasattr(self.ollama, "tags") else None
        r = self.run_from_checkout("uninstall", "--purge", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("does not touch Ollama", r.stdout)
        self.assertFalse(data.exists())
        if tags is not None:
            self.assertEqual(self.ollama.tags(), tags)

    def run_from_checkout(self, *args):
        env = self.environ()
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run([sys.executable, "-m", "baabaa", *args], capture_output=True, text=True, timeout=120,
                              env=env, cwd=str(self.dir))

    def test_installer_refuses(self):
        r = self.run_cmd(["sh", str(ROOT / "install.sh"), "not-a-version"], check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown argument", r.stderr)
        r = self.run_cmd(["sh", str(ROOT / "install.sh")], check=False)  # nothing published in the feed
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing was installed", r.stderr)
        rel = make_release(self.feed, "9.9.1")
        publish(self.feed, [dict(rel, sha256="1" * 64)])
        r = self.run_cmd(["sh", str(ROOT / "install.sh")], check=False)
        self.assertIn("does not match its SHA-256", r.stderr)
        self.assertFalse((self.home / ".local" / "lib" / "baabaa" / "versions").exists())
        # a checkout does not update itself
        r = self.run_from_checkout("update")
        self.assertEqual(r.returncode, 1)
        self.assertIn("git pull", r.stdout)


# the update API and who may use it ------------------------------------------------------------------------------------
class TestUpdateApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved = {k: os.environ.get(k) for k in ("BAABAA_HOME", "BAABAA_OLLAMA_URL")}
        os.environ["BAABAA_HOME"] = cls.tmp.name
        os.environ["BAABAA_OLLAMA_URL"] = "http://127.0.0.1:9"
        from baabaa.app import App
        from baabaa.server.api import Web
        from baabaa.server.http import Server
        from baabaa.server.lan import LanGuard
        cls.loop = asyncio.new_event_loop()
        started = threading.Event()

        def run():
            asyncio.set_event_loop(cls.loop)
            cls.app = App()
            cls.app.maindb.set_setting("network", "lan")  # an owner who opened baabaa to the network
            web = Web(cls.app, LanGuard(bind=["127.0.0.1"], networks=[]), tls=False, port=0, setup_token="tok")
            s = cls.loop.run_until_complete(Server(web).listen_tcp("127.0.0.1", 0))
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
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls.tmp.cleanup()

    def req(self, method, path, body=None, who=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}"}
        if body is not None:
            h["Content-Type"] = "application/json"
        if who:
            h["Cookie"], h["X-CSRF-Token"] = who
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        return r.status, (json.loads(data) if data else None), r

    def session(self, r, d):
        return r.getheader("Set-Cookie").split(";")[0], d["csrf"]

    def test_permissions_and_waiting(self):
        st, d, r = self.req("POST", "/api/setup", {"name": "owner", "display_name": "Owner", "token": "tok"})
        self.assertEqual(st, 200)
        owner = self.session(r, d)
        st, d, _ = self.req("POST", "/api/accounts", {"name": "lamb", "display_name": "Lamb"}, owner)
        self.assertEqual(st, 200, d)
        st, d, r = self.req("POST", "/api/login", {"name": "lamb"})
        member = self.session(r, d)

        st, d, _ = self.req("GET", "/api/update", None, owner)
        self.assertEqual(st, 200)
        self.assertEqual((d["kind"], d["state"], d["can_act"], d["owner"], d["restart_by"]), ("git", "up_to_date", True, True, "all"))
        self.assertTrue(d["running_notes"])
        st, d, _ = self.req("GET", "/api/update", None, member)
        self.assertTrue(d["can_act"])
        self.assertNotIn("previous", d)
        # owners only: checks, settings
        self.assertEqual(self.req("POST", "/api/update/check", {}, member)[0], 403)
        self.assertEqual(self.req("PATCH", "/api/update/settings", {"restart_by": "all"}, member)[0], 403)

        # a member may restart (the default), but not again right after a restart
        st, d, _ = self.req("POST", "/api/restart", {}, member)
        self.assertEqual(st, 409)
        self.assertIn("a few minutes ago", d["error"])
        self.app.started_ms -= 10 * 60_000
        st, d, _ = self.req("POST", "/api/restart", {}, member)
        self.assertEqual(st, 200, d)
        self.assertEqual((d["pending"]["reason"], d["pending"]["now"], d["pending"]["by"]), ("restart", False, "Lamb"))
        # while it waits, no new replies start
        st, d, _ = self.req("POST", "/api/conversations", {}, owner)
        cid = d["conversation"]["id"]
        st, d, _ = self.req("POST", f"/api/conversations/{cid}/messages", {"text": "hi"}, owner)
        self.assertEqual(st, 409)
        self.assertIn("about to restart", d["error"])
        st, d, _ = self.req("GET", "/api/update", None, member)
        self.assertEqual(d["pending"]["phase"], "waiting")
        self.assertEqual(d["work"], {"replies": 0, "yours": [], "jobs": []})
        self.assertEqual(self.req("DELETE", "/api/restart", None, member)[0], 200)
        self.assertIsNone(self.req("GET", "/api/update", None, owner)[1]["pending"])

        # an owner limits restarts to owners
        st, d, _ = self.req("PATCH", "/api/update/settings", {"restart_by": "owners"}, owner)
        self.assertEqual((st, d["restart_by"]), (200, "owners"))
        self.assertFalse(self.req("GET", "/api/update", None, member)[1]["can_act"])
        self.assertEqual(self.req("POST", "/api/restart", {}, member)[0], 403)
        self.assertEqual(self.req("POST", "/api/update/install", {}, member)[0], 403)
        self.assertEqual(self.req("POST", "/api/restart", {}, owner)[0], 200)
        self.assertEqual(self.req("DELETE", "/api/restart", None, owner)[0], 200)
        # nothing to install on a checkout
        st, d, _ = self.req("POST", "/api/update/install", {}, owner)
        self.assertEqual(st, 409)
        # the local terminal's restart needs the socket and the local key, never TCP
        self.assertEqual(self.req("POST", "/api/local/restart", {})[0], 403)

        # who may connect: owners choose; a restart takes it to the new address
        self.assertEqual(self.req("GET", "/api/network", None, member)[0], 403)
        st, d, _ = self.req("GET", "/api/network", None, owner)
        self.assertEqual((st, d["saved"], d["running"], d["next_urls"]), (200, "lan", "lan", None))
        self.assertEqual(sorted(d["passwordless"]), ["Lamb", "Owner"])
        self.assertFalse(d["remote"])
        st, d, _ = self.req("PATCH", "/api/network", {"mode": "local"}, owner)
        self.assertEqual((d["saved"], d["running"]), ("local", "lan"))
        self.assertEqual(d["next_urls"], ["http://localhost:0/"])
        self.assertEqual(self.req("PATCH", "/api/network", {"mode": "everyone"}, owner)[0], 400)
        st, d, _ = self.req("POST", "/api/restart", {}, owner)
        self.assertEqual((d["pending"]["reason"], d["pending"]["next_url"]), ("network", "http://localhost:0/"))
        self.assertEqual(self.req("DELETE", "/api/restart", None, owner)[0], 200)
        st, d, _ = self.req("PATCH", "/api/network", {"mode": "lan"}, owner)
        self.assertIsNone(d["next_urls"])


if __name__ == "__main__":
    unittest.main()
