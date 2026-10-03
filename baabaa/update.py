"""Installing and updating baabaa: where this copy runs from, the signed release list, downloads, the
versions side by side, and the switch between them.

An installed copy lives in PREFIX (default ~/.local/lib/baabaa, or $BAABAA_PREFIX):

    versions/<version>/   one folder per version: the release's files, unpacked
    current               a link to the version the `baabaa` command runs; switched in one atomic step
    update.json           channel, checks on or off, the last check, a downloaded version, the previous one
    downloads/            downloads in progress

The `baabaa` command (~/.local/bin/baabaa) links to PREFIX/current/bin/baabaa, and a running server keeps
the version it started with until it restarts. Updates never touch the data folder (paths.py), except for
a copy of its databases taken before each switch (backups/, the last two kept).

The release list (releases.json) is signed with the maintainer's Ed25519 key (signing.py). An update is
used only when that signature checks out and the download matches the list's SHA-256 and size.
A copy that runs from a git checkout, or from any folder outside PREFIX, does not update itself.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, signing

REPO = "https://github.com/kristiandroste/baabaa"
FEED = REPO + "/releases/latest/download"   # releases.json, releases.json.sig and install.sh
STABLE_DAYS = 7          # the stable channel gets a release after it has been out this long without being pulled
CHECK_EVERY_S = 86400    # the server's background check
KEEP_VERSIONS = 3        # versions kept on disk besides any a running server uses
KEEP_BACKUPS = 2
MAX_LIST_BYTES = 2_000_000
MAX_RELEASE_BYTES = 200_000_000
CHANNELS = ("stable", "latest")


class UpdateError(Exception):
    pass


class NoReleases(UpdateError):
    """No release list is published at the feed (yet)."""


# where things are ------------------------------------------------------------------------------------
def prefix() -> Path:
    env = os.environ.get("BAABAA_PREFIX")
    return Path(env).expanduser() if env else Path.home() / ".local" / "lib" / "baabaa"


def bin_dir() -> Path:
    env = os.environ.get("BAABAA_BIN_DIR")
    return Path(env).expanduser() if env else Path.home() / ".local" / "bin"


def code_root() -> Path:
    """The folder that holds this copy's `baabaa` package (and bin/, README.md, CHANGELOG.md)."""
    return Path(__file__).resolve().parent.parent


def kind(root: Path | None = None) -> str:
    """'installed' (a version in PREFIX), 'git' (a git checkout) or 'folder' (any other copy)."""
    root = (root or code_root()).resolve()
    try:
        if root.parent == (prefix() / "versions").resolve():
            return "installed"
    except OSError:
        pass
    return "git" if (root / ".git").exists() else "folder"


def installed_version() -> str | None:
    """The version PREFIX/current points to, or None when nothing is installed there."""
    try:
        target = os.readlink(prefix() / "current")
    except OSError:
        return None
    name = Path(target).name
    return name if (prefix() / "versions" / name / "bin" / "baabaa").exists() else None


def versions_on_disk() -> list[str]:
    d = prefix() / "versions"
    if not d.is_dir():
        return []
    found = [p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith(".") and (p / "bin" / "baabaa").exists()]
    return sorted(found, key=version_key)


def disabled() -> str | None:
    """Why updates are off for this copy, or None."""
    if os.environ.get("BAABAA_DISABLE_UPDATES") == "1":
        return "Updates are turned off on this computer (BAABAA_DISABLE_UPDATES)."
    k = kind()
    if k == "git":
        return "This baabaa runs from a git checkout: update it with git pull."
    if k == "folder":
        return "This baabaa runs from a folder of its own, not an installed copy: install it with the installer to get updates."
    return None


# versions -------------------------------------------------------------------------------------------
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?$")


def valid_version(text: str) -> bool:
    return bool(_VERSION.match(text or ""))


def version_key(text: str) -> tuple:
    """Sort key: 1.2.3 > 1.2.3-rc.1 > 1.2.2. Anything unparseable sorts first."""
    m = _VERSION.match(text or "")
    if not m:
        return (-1,)
    pre = m.group(4)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 1 if pre is None else 0, pre or "")


def newer(a: str | None, b: str | None) -> bool:
    """True when version `a` is newer than `b` (a missing `b` is older than anything)."""
    if not a:
        return False
    return b is None or version_key(a) > version_key(b)


# state ----------------------------------------------------------------------------------------------
DEFAULT_STATE = {"channel": "stable", "checks": True, "download": True}


def load_state() -> dict:
    try:
        data = json.loads((prefix() / "update.json").read_text())
    except (OSError, ValueError):
        data = {}
    return {**DEFAULT_STATE, **(data if isinstance(data, dict) else {})}


def save_state(state: dict) -> None:
    p = prefix()
    p.mkdir(parents=True, exist_ok=True)
    tmp = p / f".update.json.{os.getpid()}"
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, p / "update.json")


def set_preferences(channel: str | None = None, checks: bool | None = None) -> dict:
    state = load_state()
    if channel is not None:
        if channel not in CHANNELS:
            raise UpdateError("The channel is stable or latest.")
        if channel != state.get("channel"):
            state["latest"] = None  # the last check was for the other channel
        state["channel"] = channel
    if checks is not None:
        state["checks"] = bool(checks)
    save_state(state)
    return state


# the release list ------------------------------------------------------------------------------------
def feed_url() -> str:
    return (os.environ.get("BAABAA_UPDATE_URL") or FEED).rstrip("/")


def _fetch(url: str, limit: int, timeout: float = 30.0, progress=None, out=None) -> bytes | int:
    """GET `url` (https, http or file). Into memory, or streamed into the open file `out` (returns the size)."""
    req = urllib.request.Request(url, headers={"User-Agent": f"baabaa/{__version__}"})
    try:
        from .util import ssl_context
        resp = urllib.request.urlopen(req, timeout=timeout, context=ssl_context() if url.startswith("https:") else None)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NoReleases(f"Not found: {url}") from None
        raise UpdateError(f"The download failed: HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc or url}") from None
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(getattr(exc, "reason", None), FileNotFoundError) or isinstance(exc, FileNotFoundError):
            raise NoReleases(f"Not found: {url}") from None
        reason = getattr(exc, "reason", exc)
        raise UpdateError(f"baabaa could not reach the release server ({reason}).") from None
    with resp:
        total = int(resp.headers.get("Content-Length") or 0) or None
        got, chunks = 0, []
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            got += len(chunk)
            if got > limit:
                raise UpdateError(f"The download is larger than expected ({got} bytes from {url}).")
            if out is not None:
                out.write(chunk)
            else:
                chunks.append(chunk)
            if progress:
                progress(got, total)
    return got if out is not None else b"".join(chunks)


def fetch_releases(url: str | None = None) -> dict:
    """The signed release list: {"format": 1, "releases": [...]}, after its signature checked out."""
    base = (url or feed_url()).rstrip("/")
    data = _fetch(base + "/releases.json", MAX_LIST_BYTES)
    try:
        sig = _fetch(base + "/releases.json.sig", 4096).decode("ascii", "replace")
    except NoReleases:
        raise UpdateError("The release list has no signature file, so baabaa does not trust it.") from None
    if signing.verify_release_file(data, sig) is None:
        raise UpdateError("The release list's signature does not check out, so baabaa does not trust it.")
    try:
        index = json.loads(data)
    except ValueError:
        raise UpdateError("The release list is not valid JSON.") from None
    if not isinstance(index, dict) or index.get("format") != 1 or not isinstance(index.get("releases"), list):
        raise UpdateError("The release list is in a format this baabaa does not know: install the new version "
                          "with the installer.")
    good = []
    for r in index["releases"]:
        if (isinstance(r, dict) and valid_version(str(r.get("version"))) and re.fullmatch(r"[0-9a-f]{64}", str(r.get("sha256")))
                and isinstance(r.get("size"), int) and 0 < r["size"] <= MAX_RELEASE_BYTES and (r.get("url") or r.get("file"))):
            good.append(r)
    index["releases"] = good
    index["base"] = base
    return index


def _released_s(r: dict) -> float:
    try:
        return time.mktime(time.strptime(str(r.get("date"))[:10], "%Y-%m-%d"))
    except ValueError:
        return time.time()


def pick(index: dict, channel: str, now: float | None = None) -> dict | None:
    """The newest release for `channel`: latest = every release; stable = one out STABLE_DAYS days, or
    marked stable when published (a first release, an urgent fix). Pulled releases never count."""
    now = time.time() if now is None else now
    out = None
    for r in index.get("releases") or []:
        if r.get("pulled"):
            continue
        if channel == "stable" and not r.get("stable") and now - _released_s(r) < STABLE_DAYS * 86400:
            continue
        if python_too_old(r):
            continue
        if out is None or version_key(r["version"]) > version_key(out["version"]):
            out = r
    return out


def python_too_old(r: dict) -> bool:
    need = str(r.get("python") or "3.10")
    try:
        return sys.version_info[:2] < tuple(int(x) for x in need.split(".")[:2])
    except ValueError:
        return False


def release_url(index: dict, r: dict) -> str:
    return urllib.parse.urljoin(index["base"] + "/", r.get("url") or r["file"])


def summary(r: dict | None) -> dict | None:
    """What windows and the terminal show about a release."""
    if not r:
        return None
    notes = r.get("notes") or []
    return {"version": r["version"], "date": str(r.get("date") or "")[:10], "notes": [str(n) for n in notes][:40],
            "size": r.get("size")}


# getting a version onto disk ---------------------------------------------------------------------------
def download(index: dict, r: dict, progress=None) -> Path:
    """Download a release into PREFIX/downloads and check its size and SHA-256."""
    d = prefix() / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    name = f"baabaa-{r['version']}.tar.gz"
    part, final = d / (name + ".part"), d / name
    with open(part, "wb") as f:
        size = _fetch(release_url(index, r), r["size"], timeout=60, progress=progress, out=f)
    if size != r["size"]:
        part.unlink(missing_ok=True)
        raise UpdateError(f"The download of baabaa {r['version']} is incomplete ({size} of {r['size']} bytes).")
    if sha256(part) != r["sha256"]:
        part.unlink(missing_ok=True)
        raise UpdateError(f"The download of baabaa {r['version']} does not match the release list (SHA-256); not using it.")
    os.replace(part, final)
    return final


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_version(root: Path) -> str | None:
    try:
        m = re.search(r'^__version__ = "([^"]+)"', (root / "baabaa" / "__init__.py").read_text(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def unpack(tarball: Path, version: str) -> Path:
    """Unpack a release (top folder baabaa-<version>/) into PREFIX/versions/<version>. Only plain files and
    folders inside that top folder are taken; anything else in the archive stops the install."""
    versions = prefix() / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    final = versions / version
    work = versions / f".{version}.partial"
    shutil.rmtree(work, ignore_errors=True)
    top = f"baabaa-{version}/"
    try:
        with tarfile.open(tarball, "r:gz") as tar:
            for m in tar.getmembers():
                name = m.name
                if name.rstrip("/") == top.rstrip("/"):
                    continue
                parts = Path(name).parts
                if not name.startswith(top) or name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()):
                    raise UpdateError(f"The release archive holds an unexpected entry ({name}); not installing it.")
                dest = work / name[len(top):]
                if m.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                with src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(dest, 0o755 if m.mode & 0o111 else 0o644)
    except (tarfile.TarError, OSError, EOFError) as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise UpdateError(f"The release archive could not be unpacked: {exc}") from None
    except UpdateError:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return _settle(work, final, version)


# What a release holds: the program, its launcher and installer, and the documents a user needs.
RELEASE_FILES = ("baabaa", "bin/baabaa", "install.sh", "README.md", "CHANGELOG.md", "LICENSE", "docs")


def copy_tree(src: Path, version: str) -> Path:
    """Copy a release's files from an unpacked release (the installer's) or a checkout into
    PREFIX/versions/<version>."""
    versions = prefix() / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    work = versions / f".{version}.partial"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")
    for name in RELEASE_FILES:
        p = src / name
        if p.is_dir():
            shutil.copytree(p, work / name, symlinks=False, ignore=skip)
        elif p.is_file():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, work / name)
    return _settle(work, prefix() / "versions" / version, version)


def _settle(work: Path, final: Path, version: str) -> Path:
    found = _tree_version(work)
    if found != version or not (work / "bin" / "baabaa").exists():
        shutil.rmtree(work, ignore_errors=True)
        raise UpdateError(f"The release does not contain baabaa {version} (it says {found or 'nothing'}).")
    os.chmod(work / "bin" / "baabaa", 0o755)
    if final.exists():
        if _tree_version(final) == version and (final / "bin" / "baabaa").exists():
            shutil.rmtree(work, ignore_errors=True)  # already there and whole
            return final
        shutil.rmtree(final)
    os.replace(work, final)
    return final


def selfcheck(root: Path, timeout: float = 120) -> tuple[bool, str]:
    """Run `baabaa selfcheck` from `root`: every module imports. A new version must pass before it runs."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "BAABAA_CLEAN_ENV")}
    env["PYTHONPATH"] = str(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        from . import BOOT
        res = subprocess.run([sys.executable, "-c", BOOT, str(root), "selfcheck"], cwd=str(root), env=env,
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    out = (res.stdout + res.stderr).strip()
    return res.returncode == 0, out[-2000:]


def switch(version: str) -> str | None:
    """Point PREFIX/current at `version` in one atomic step. Returns the version it pointed to before."""
    before = installed_version()
    if not (prefix() / "versions" / version / "bin" / "baabaa").exists():
        raise UpdateError(f"baabaa {version} is not on this computer.")
    tmp = prefix() / f".current.{os.getpid()}"
    tmp.unlink(missing_ok=True)
    os.symlink(f"versions/{version}", tmp)
    os.replace(tmp, prefix() / "current")
    if before and before != version:
        state = load_state()
        state["previous"] = before
        if state.get("staged") == version:
            state["staged"] = None
        save_state(state)
    return before


def ensure_launcher() -> tuple[Path, str]:
    """The `baabaa` command: BIN/baabaa linking to PREFIX/current/bin/baabaa. A command someone put there
    themselves (a file, or a link elsewhere) is left alone. Returns (path, what happened)."""
    d = bin_dir()
    d.mkdir(parents=True, exist_ok=True)
    link, target = d / "baabaa", prefix() / "current" / "bin" / "baabaa"
    if link.is_symlink():
        current = os.readlink(link)
        if Path(current) == target:
            return link, "kept"
        if not str(Path(current)).startswith(str(prefix())):
            return link, "custom"
    elif link.exists():
        return link, "custom"
    tmp = d / f".baabaa.{os.getpid()}"
    tmp.unlink(missing_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, link)
    return link, "made"


def cleanup(running: set[str] | None = None) -> list[str]:
    """Delete old versions: keep the current and previous ones, any a running server uses, and the newest
    KEEP_VERSIONS. Also clears unfinished unpacks and downloads. Returns the versions deleted."""
    keep = set(running or ())
    cur = installed_version()
    state = load_state()
    keep |= {v for v in (cur, state.get("previous"), state.get("staged")) if v}
    all_versions = versions_on_disk()
    keep |= set(all_versions[-KEEP_VERSIONS:])
    gone = []
    for v in all_versions:
        if v not in keep:
            shutil.rmtree(prefix() / "versions" / v, ignore_errors=True)
            gone.append(v)
    d = prefix() / "versions"
    if d.is_dir():
        for p in d.iterdir():
            if p.name.startswith(".") and p.name.endswith(".partial") and time.time() - p.stat().st_mtime > 3600:
                shutil.rmtree(p, ignore_errors=True)
    dl = prefix() / "downloads"
    if dl.is_dir():
        for p in dl.iterdir():
            if not any(p.name == f"baabaa-{v}.tar.gz" for v in keep) or p.name.endswith(".part"):
                p.unlink(missing_ok=True)
    return gone


# the data folder's databases ----------------------------------------------------------------------------
def backup_databases(data_root: Path, label: str) -> Path | None:
    """Copy the data folder's SQLite databases (consistently, with SQLite's backup) into backups/<label>-<time>/,
    keeping the newest KEEP_BACKUPS copies."""
    dbs = [p for p in [data_root / "baabaa.db", data_root / "stats.db"] if p.exists()]
    accounts = data_root / "accounts"
    if accounts.is_dir():
        dbs += sorted(accounts.glob("*/account.db"))
    if not dbs:
        return None
    root = data_root / "backups"
    dest = root / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
    for src in dbs:
        out = dest / src.relative_to(data_root)
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        s = sqlite3.connect(str(src), timeout=30)
        d = sqlite3.connect(str(out))
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        os.chmod(out, 0o600)
    old = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for p in old[:-KEEP_BACKUPS]:
        shutil.rmtree(p, ignore_errors=True)
    return dest


# checking, staging, applying ----------------------------------------------------------------------------
def check(download_too: bool | None = None, progress=None) -> dict:
    """Ask the release list for this copy's channel. A newer version is downloaded and unpacked ahead of
    time (unless turned off), so updating is only the switch and a restart. Returns the saved state."""
    state = load_state()
    state["checked_ms"] = int(time.time() * 1000)
    why = disabled()
    if why:
        state["error"] = why
        return state
    try:
        index = fetch_releases()
    except NoReleases:
        state.update(latest=None, error=None, published=False)
        save_state(state)
        return state
    except UpdateError as exc:
        state["error"] = str(exc)
        save_state(state)
        return state
    target = pick(index, state.get("channel") or "stable")
    state.update(latest=summary(target), error=None, published=True)
    current = installed_version()
    want = state.get("download", True) if download_too is None else download_too
    if target and newer(target["version"], current) and want and target["version"] != state.get("failed"):
        try:
            stage(index, target, progress)
            state["staged"] = target["version"]
        except UpdateError as exc:
            state["error"] = str(exc)
    save_state(state)
    return state


def stage(index: dict, r: dict, progress=None) -> Path:
    """A release on disk and checked, ready to switch to."""
    final = prefix() / "versions" / r["version"]
    if _tree_version(final) == r["version"] and (final / "bin" / "baabaa").exists():
        return final
    tarball = download(index, r, progress)
    try:
        final = unpack(tarball, r["version"])
    finally:
        tarball.unlink(missing_ok=True)
    ok, out = selfcheck(final)
    if not ok:
        shutil.rmtree(final, ignore_errors=True)
        raise UpdateError(f"baabaa {r['version']} did not pass its start-up check, so it was not installed:\n{out}")
    return final


def fetch_version(version: str, progress=None) -> dict:
    """A given version (or channel) from the release list onto disk. Returns its release entry."""
    index = fetch_releases()
    if version in CHANNELS:
        r = pick(index, version)
        if r is None and version == "stable":
            r = pick(index, "latest")
    else:
        r = next((x for x in index["releases"] if x["version"] == version), None)
        if r is not None and r.get("pulled"):
            raise UpdateError(f"baabaa {version} was withdrawn; choose another version.")
    if r is None:
        raise UpdateError(f"There is no release {version}.")
    if python_too_old(r):
        raise UpdateError(f"baabaa {r['version']} needs Python {r.get('python')} or newer.")
    stage(index, r, progress)
    return r


def status(running: str = __version__, changed: bool = False) -> dict:
    """What this copy should tell people about updates.

    state: "up_to_date"; "available" (a newer release, not downloaded: Install); "ready" (downloaded and
    checked: Restart to update); "installed" (the installed version is newer than the running one: Restart);
    "changed" (the files of a non-installed copy changed since it started: Restart)."""
    k = kind()
    state = load_state() if k == "installed" else dict(DEFAULT_STATE)
    installed = installed_version() if k == "installed" else running
    latest = state.get("latest")
    out = {"kind": k, "running": running, "installed": installed, "channel": state.get("channel", "stable"),
           "checks": bool(state.get("checks", True)), "checked_ms": state.get("checked_ms"), "error": state.get("error"),
           "published": state.get("published"), "disabled": disabled(), "latest": None, "state": "up_to_date",
           "previous": None}
    prev = state.get("previous")
    if k == "installed" and prev and prev != installed and (prefix() / "versions" / prev / "bin" / "baabaa").exists():
        out["previous"] = prev
    if latest and newer(latest.get("version"), installed) and latest.get("version") != state.get("failed"):
        out["latest"] = latest
        staged = state.get("staged")
        ready = staged == latest["version"] and (prefix() / "versions" / staged / "bin" / "baabaa").exists()
        out["state"] = "ready" if ready else "available"
    if k == "installed" and installed and installed != running:
        out["state"] = "installed"  # newer, or an older one switched back to: a restart runs it
        out["target"] = installed
    elif k != "installed" and changed:
        out["state"] = "changed"
    if out["state"] in ("available", "ready"):
        out["target"] = latest["version"]
    return out


# the code a server started with ---------------------------------------------------------------------------
def fingerprint(root: Path | None = None) -> str:
    """Names, sizes and times of this copy's files: changes when the code on disk changes (a git pull)."""
    root = root or code_root()
    h = hashlib.sha256()
    for base in (root / "baabaa", root / "bin"):
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for name in sorted(filenames):
                if name.endswith((".pyc", ".pyo")):
                    continue
                p = os.path.join(dirpath, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                h.update(f"{os.path.relpath(p, root)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def target_root() -> Path:
    """Where a restart takes its code from: the current installed version, or this same folder."""
    if kind() == "installed" and installed_version():
        return (prefix() / "versions" / installed_version()).resolve()
    return code_root()


def changelog(root: Path | None = None, version: str | None = None) -> list[str]:
    """The notes for `version` from CHANGELOG.md ("## 1.2.3 ..." sections, "- " items)."""
    root = root or code_root()
    version = version or __version__
    try:
        text = (root / "CHANGELOG.md").read_text()
    except OSError:
        return []
    notes, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].split()[0].strip("[]v") == version if line[3:].split() else False
            continue
        if inside and line.startswith("- "):
            notes.append(line[2:].strip())
        elif inside and line.startswith("  ") and notes:
            notes[-1] += " " + line.strip()
    return notes


def tempdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="baabaa-"))
