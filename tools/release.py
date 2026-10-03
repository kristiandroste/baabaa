#!/usr/bin/env python3
"""Making a baabaa release (for the maintainer; not part of the release itself). docs/RELEASING.md explains it.

  python3 tools/release.py check                     private-words scan and the tests
  python3 tools/release.py build [--stable] [--local] [--skip-tests]
  python3 tools/release.py pull VERSION               withdraw a release: the list marks it, and it is signed again
  python3 tools/release.py publish VERSION [--go]     upload to GitHub with gh (prints the commands without --go)

`build` writes dist/<version>/: baabaa-<version>.tar.gz (the same bytes from the same files, every time),
releases.json and its signature, install.sh, and baabaa-<version>-install.sh (the installer with the program
inside, to hand to someone directly). dist/releases.json keeps the list of every release so far.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from baabaa import signing, update  # noqa: E402

CONFIG = Path(os.environ.get("BAABAA_RELEASE_CONFIG", Path.home() / ".config" / "baabaa-release"))
KEY = CONFIG / "signing-key.pem"
PRIVATE_WORDS = CONFIG / "private-words.txt"
DIST = ROOT / "dist"
SLUG = "kristiandroste/baabaa"


def version() -> str:
    m = re.search(r'^__version__ = "([^"]+)"', (ROOT / "baabaa" / "__init__.py").read_text(), re.M)
    return m.group(1)


def release_date(v: str) -> str:
    """The date on the version's CHANGELOG heading ("## 1.2.3 - 2026-10-02")."""
    for line in (ROOT / "CHANGELOG.md").read_text().splitlines():
        if line.startswith("## ") and line[3:].split()[0].strip("[]v") == v:
            m = re.search(r"\d{4}-\d{2}-\d{2}", line)
            if m:
                return m.group(0)
    sys.exit(f"CHANGELOG.md has no dated section for {v} (\"## {v} - YYYY-MM-DD\").")


# what goes in ----------------------------------------------------------------------------------------------------
def release_files() -> list[Path]:
    out = []
    for name in update.RELEASE_FILES:
        p = ROOT / name
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts and not f.name.endswith((".pyc", ".pyo")):
                    out.append(f)
        elif p.is_file():
            out.append(p)
    return out


def repo_files() -> list[Path]:
    """Every file git would publish: tracked ones and new ones it does not ignore."""
    res = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
                         capture_output=True, check=True)
    return [ROOT / n.decode() for n in res.stdout.split(b"\0") if n and (ROOT / n.decode()).is_file()]


# checks ------------------------------------------------------------------------------------------------------------
def private_scan() -> list[str]:
    """Words that must never be published (names, addresses, this machine's user and host names, private
    folders), from a list kept outside the repository: one regular expression per line, # for comments.
    Looks in every file git would publish and in the whole commit history: authors, messages and every change,
    because a word removed in a later commit is still published with the earlier one."""
    if not PRIVATE_WORDS.exists():
        return [f"{PRIVATE_WORDS} is missing: create it (one pattern per line) so releases can be checked"]
    pats = []
    for line in PRIVATE_WORDS.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pats.append(re.compile(line, re.I))
    hits = []
    for f in sorted(set(repo_files()) | set(release_files())):
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            text = f.read_bytes().decode("latin-1")
        for i, line in enumerate(text.splitlines(), 1):
            for p in pats:
                if p.search(line):
                    hits.append(f"{f.relative_to(ROOT)}:{i}: matches /{p.pattern}/")
    return hits + history_scan(pats, ROOT)


def history_scan(pats: list, root: Path) -> list[str]:
    """Matches in every commit of every branch: its author, committer, message and the lines it changed."""
    log = subprocess.run(["git", "log", "--all", "-p", "--no-color", "--format=%x00%h%n%an%n%ae%n%cn%n%ce%n%B"],
                         cwd=root, capture_output=True)
    hits, commit = [], "?"
    for line in log.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("\0"):
            commit, line = line[1:], ""
        for p in pats:
            hit = f"git history, commit {commit}: matches /{p.pattern}/"
            if hit not in hits and p.search(line):
                hits.append(hit)
    return hits


def run_tests() -> None:
    print("Running the tests…", flush=True)
    res = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT)
    if res.returncode != 0:
        sys.exit("The tests failed; no release.")
    if shutil.which("node"):
        for t in sorted((ROOT / "tests" / "js").glob("*.test.mjs")):
            if subprocess.run(["node", str(t)], cwd=ROOT, capture_output=True).returncode != 0:
                sys.exit(f"{t.name} failed; no release.")


def check(args) -> None:
    hits = private_scan()
    for h in hits:
        print(h)
    if hits:
        sys.exit("Private words found; nothing may be published until they are gone.")
    print("No private words in the files git would publish or in the history.")
    if not getattr(args, "skip_tests", False):
        run_tests()


# building ------------------------------------------------------------------------------------------------------------
def tarball(v: str, epoch: int) -> bytes:
    """The release archive, byte for byte the same from the same files: sorted entries, one timestamp, no
    owner names, normalised permissions, gzip without a time or file name."""
    buf = io.BytesIO()
    top = f"baabaa-{v}"
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        dirs = set()
        files = release_files()
        for f in files:
            rel = f.relative_to(ROOT)
            for parent in list(rel.parents)[::-1][1:]:
                dirs.add(parent)
        entries = [(d, True) for d in sorted(dirs)] + [(f.relative_to(ROOT), False) for f in files]
        entries.sort(key=lambda e: str(e[0]))
        root_info = tarfile.TarInfo(top)
        root_info.type, root_info.mode, root_info.mtime = tarfile.DIRTYPE, 0o755, epoch
        tar.addfile(root_info)
        for rel, is_dir in entries:
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.mtime, info.uid, info.gid, info.uname, info.gname = epoch, 0, 0, "", ""
            if is_dir:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            else:
                data = (ROOT / rel).read_bytes()
                info.size = len(data)
                info.mode = 0o755 if os.access(ROOT / rel, os.X_OK) else 0o644
                tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz:
        gz.write(buf.getvalue())
    return out.getvalue()


def sign(data: bytes) -> str:
    if not KEY.exists():
        sys.exit(f"The signing key {KEY} is missing.")
    import tempfile
    with tempfile.NamedTemporaryFile() as f:  # -rawin needs a file of known size, not a pipe
        f.write(data)
        f.flush()
        res = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(KEY), "-rawin", "-in", f.name], capture_output=True)
    if res.returncode != 0 or len(res.stdout) != 64:
        sys.exit(f"openssl could not sign: {res.stderr.decode(errors='replace').strip()}")
    pub = subprocess.run(["openssl", "pkey", "-in", str(KEY), "-pubout", "-outform", "DER"], capture_output=True, check=True).stdout[-32:]
    kid = signing.key_id(pub.hex())
    if signing.RELEASE_KEYS.get(kid) != pub.hex():
        sys.exit(f"This key ({kid}) is not in signing.RELEASE_KEYS, so installed copies would not trust it.")
    text = f"{kid} {res.stdout.hex()}\n"
    if signing.verify_release_file(data, text) != kid:
        sys.exit("The new signature does not verify; nothing was written.")
    return text


def load_index() -> dict:
    p = DIST / "releases.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"format": 1, "name": "baabaa", "releases": []}


def write_index(index: dict, out: Path) -> None:
    index["generated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    index["releases"].sort(key=lambda r: update.version_key(r["version"]), reverse=True)
    data = (json.dumps(index, indent=1, ensure_ascii=False) + "\n").encode()
    sig = sign(data)
    for d in {DIST, out}:
        d.mkdir(parents=True, exist_ok=True)
        (d / "releases.json").write_bytes(data)
        (d / "releases.json.sig").write_text(sig)


def bundle_script(v: str, data: bytes) -> bytes:
    """The installer with the release inside: install.sh with the version and checksum filled in, then the
    archive after a marker line (the script ends before it, so the shell never reads it)."""
    sha = hashlib.sha256(data).hexdigest()
    script = (ROOT / "install.sh").read_text()
    script = script.replace('BUNDLE_VERSION=""', f'BUNDLE_VERSION="{v}"', 1).replace('BUNDLE_SHA256=""', f'BUNDLE_SHA256="{sha}"', 1)
    return script.encode() + b"\n__BAABAA_PAYLOAD__\n" + data


def build(args) -> None:
    v = version()
    if not update.valid_version(v):
        sys.exit(f"{v} is not a version number like 1.2.3.")
    date = release_date(v)
    notes = update.changelog(ROOT, v)
    if not notes:
        sys.exit(f"CHANGELOG.md has no notes under {v}.")
    check(args)
    import calendar
    epoch = calendar.timegm(time.strptime(date, "%Y-%m-%d"))  # midnight UTC: the same bytes in any time zone
    data = tarball(v, epoch)
    if tarball(v, epoch) != data:
        sys.exit("The archive is not the same when built twice.")
    out = DIST / v
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    name = f"baabaa-{v}.tar.gz"
    (out / name).write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    index = load_index()
    index["releases"] = [r for r in index["releases"] if r["version"] != v]
    url = name if args.local else f"https://github.com/{SLUG}/releases/download/v{v}/{name}"
    index["releases"].append({"version": v, "date": date, "file": name, "url": url, "sha256": sha, "size": len(data),
                              "python": "3.10", "notes": notes, "stable": bool(args.stable)})
    write_index(index, out)
    shutil.copy2(ROOT / "install.sh", out / "install.sh")
    bundle = out / f"baabaa-{v}-install.sh"
    bundle.write_bytes(bundle_script(v, data))
    os.chmod(bundle, 0o755)
    print(f"Built baabaa {v} in {out}:")
    for f in sorted(out.iterdir()):
        print(f"  {f.name:32} {f.stat().st_size:>9,} bytes")
    print(f"SHA-256 of the archive: {sha}")


def pull(args) -> None:
    index = load_index()
    hit = [r for r in index["releases"] if r["version"] == args.version]
    if not hit:
        sys.exit(f"There is no release {args.version} in {DIST / 'releases.json'}.")
    hit[0]["pulled"] = True
    newest = max((r["version"] for r in index["releases"]), key=update.version_key)
    write_index(index, DIST / newest)
    print(f"{args.version} is withdrawn in the release list. Publish the list again: tools/release.py publish {newest} --list-only")


def publish(args) -> None:
    out = DIST / args.version
    if not out.is_dir():
        sys.exit(f"Build {args.version} first.")
    hits = private_scan()
    if hits:
        sys.exit("Private words found; run tools/release.py check.")
    files = ["releases.json", "releases.json.sig"] if args.list_only else \
        [f"baabaa-{args.version}.tar.gz", "install.sh", "releases.json", "releases.json.sig"]
    paths = [str(out / f) for f in files]
    if args.list_only:
        cmd = ["gh", "release", "upload", f"v{args.version}", *paths, "--clobber", "--repo", SLUG]
    else:
        notes = "\n".join(f"- {n}" for n in update.changelog(ROOT, args.version))
        cmd = ["gh", "release", "create", f"v{args.version}", *paths, "--repo", SLUG, "--title", f"baabaa {args.version}",
               "--notes", notes]
    if not args.go:
        print("Would run (add --go to run it):\n  " + " ".join(cmd[:4]) + " … " + " ".join(cmd[-4:]))
        return
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Make a baabaa release.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--skip-tests", action="store_true")
    b = sub.add_parser("build")
    b.add_argument("--stable", action="store_true", help="on the stable channel at once (a first release, an urgent fix)")
    b.add_argument("--local", action="store_true", help="file names instead of GitHub addresses (a folder or LAN feed)")
    b.add_argument("--skip-tests", action="store_true")
    p = sub.add_parser("pull")
    p.add_argument("version")
    pu = sub.add_parser("publish")
    pu.add_argument("version")
    pu.add_argument("--list-only", action="store_true", help="upload only the release list (after pull)")
    pu.add_argument("--go", action="store_true")
    args = ap.parse_args()
    {"check": check, "build": build, "pull": pull, "publish": publish}[args.cmd](args)


if __name__ == "__main__":
    main()
