#!/bin/sh
# baabaa installer, for Linux and for Macs with Apple silicon. Puts baabaa in ~/.local/lib/baabaa and the
# `baabaa` command in ~/.local/bin, then asks who may connect and whether to start it. No sudo and no pip:
# baabaa needs only Python 3.10 or newer (and Ollama for models).
#
#   curl -fsSL https://github.com/kristiandroste/baabaa/releases/latest/download/install.sh | sh
#   curl -fsSL https://github.com/kristiandroste/baabaa/releases/latest/download/install.sh | sh -s -- latest
#
# The argument is a release channel (stable, the default, or latest) or a version number. Running it again
# updates baabaa. A self-contained copy of this script (baabaa-VERSION-install.sh) carries the program
# inside it and installs without downloading anything.
set -eu

BUNDLE_VERSION=""
BUNDLE_SHA256=""
FEED="${BAABAA_UPDATE_URL:-https://github.com/kristiandroste/baabaa/releases/latest/download}"
TARGET="${1:-stable}"

say() { printf '%s\n' "$*"; }
fail() { printf 'baabaa install: %s\n' "$*" >&2; exit 1; }

case "$TARGET" in
  stable|latest) ;;
  [0-9]*.[0-9]*.[0-9]*) ;;
  -h|--help) say "Usage: sh install.sh [stable|latest|VERSION]"; exit 0 ;;
  *) fail "unknown argument '$TARGET' (use stable, latest or a version number)" ;;
esac
[ -z "$BUNDLE_VERSION" ] || TARGET="$BUNDLE_VERSION"

OS="$(uname -s)"
case "$OS" in
  Linux) ;;
  Darwin)
    if [ "$(uname -m)" != arm64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" != 1 ]; then
      say "This Mac has an Intel processor. baabaa installs and its interface works, but Ollama computes on the"
      say "CPU on Intel Macs, so baabaa will not run models here."
    fi ;;
  *) fail "baabaa runs on Linux and on Macs, and this is $OS." ;;
esac

if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
  fail "do not run this with sudo: baabaa installs into your own home folder. Run it again without sudo."
fi

# A Python 3.10 or newer, without waking macOS's "install the command line developer tools" stub.
usable() {
  p="$(command -v "$1" 2>/dev/null)" || return 1
  if [ "$p" = /usr/bin/python3 ] && [ "$OS" = Darwin ] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
    return 1
  fi
  "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null
}
PY=""
for p in "${BAABAA_PYTHON:-}" python3 python3.14 python3.13 python3.12 python3.11 python3.10 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 /usr/bin/python3; do
  [ -n "$p" ] || continue
  if usable "$p"; then PY="$(command -v "$p")"; break; fi
done
if [ -z "$PY" ]; then
  hint="install Python 3.10 or newer"
  if [ "$OS" = Darwin ]; then hint="get it from https://www.python.org/downloads/macos/ or run: brew install python"
  elif command -v apt-get >/dev/null 2>&1; then hint="sudo apt install python3"
  elif command -v dnf >/dev/null 2>&1; then hint="sudo dnf install python3"
  elif command -v pacman >/dev/null 2>&1; then hint="sudo pacman -S python"
  fi
  fail "baabaa needs Python 3.10 or newer. Install it ($hint), then run this again."
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/baabaa-install.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT INT TERM

# Downloads go through curl or wget, which use the system's certificates.
fetch() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then wget -q -O "$2" "$1"
  else fail "curl or wget is needed to download baabaa."
  fi
}

# Python reads the release list, checks the archive and unpacks it (steps: pick, unpack).
helper() {
  "$PY" - "$@" <<'PYTHON'
import hashlib, json, os, re, sys, tarfile, time, urllib.parse
step = sys.argv[1]

def key(v):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?$", str(v))
    return (int(m[1]), int(m[2]), int(m[3]), 1 if m[4] is None else 0, m[4] or "") if m else (-1,)

def python_ok(r):
    need = str(r.get("python") or "3.10").split(".")
    return sys.version_info[:2] >= (int(need[0]), int(need[1]))

def age_days(r):
    try:
        return (time.time() - time.mktime(time.strptime(str(r.get("date"))[:10], "%Y-%m-%d"))) / 86400
    except ValueError:
        return 0

if step == "pick":  # releases.json, feed, target -> "version url sha256 size"
    path, feed, target = sys.argv[2:5]
    try:
        index = json.load(open(path))
    except ValueError:
        sys.exit("the release list is not readable")
    releases = [r for r in index.get("releases", []) if not r.get("pulled") and python_ok(r)]
    if target == "latest":
        pool = releases
    elif target == "stable":
        pool = [r for r in releases if r.get("stable") or age_days(r) >= 7] or releases
    else:
        pool = [r for r in releases if r.get("version") == target]
    if not pool:
        sys.exit(f"there is no release {target} for Python {sys.version_info[0]}.{sys.version_info[1]}")
    r = max(pool, key=lambda r: key(r["version"]))
    print(r["version"], urllib.parse.urljoin(feed + "/", r.get("url") or r["file"]), r["sha256"], r["size"])
elif step == "payload":  # this script with the archive inside -> the archive
    src, out = sys.argv[2:4]
    raw = open(src, "rb").read()
    mark = b"\n__BAABAA_PAYLOAD__\n"
    open(out, "wb").write(raw[raw.index(mark) + len(mark):])
elif step == "unpack":  # archive, version, sha256, size, folder
    archive, version, sha, size, dest = sys.argv[2:7]
    data = open(archive, "rb").read()
    if (size != "-" and len(data) != int(size)) or hashlib.sha256(data).hexdigest() != sha:
        sys.exit("the download does not match its SHA-256 checksum, so nothing was installed")
    top = f"baabaa-{version}/"
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            parts = m.name.split("/")
            if not (m.name + "/").startswith(top) or ".." in parts or m.name.startswith("/") or not (m.isfile() or m.isdir()):
                sys.exit(f"the release holds an unexpected entry ({m.name}), so nothing was installed")
            target = os.path.join(dest, m.name)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with tar.extractfile(m) as f, open(target, "wb") as out:
                out.write(f.read())
            os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)
PYTHON
}

if [ -n "$BUNDLE_VERSION" ]; then
  say "Installing baabaa $BUNDLE_VERSION from this file."
  VERSION="$BUNDLE_VERSION"
  SHA="$BUNDLE_SHA256"
  SIZE="-"
  helper payload "$0" "$TMP/baabaa.tar.gz" || fail "this file is damaged, so nothing was installed."
else
  fetch "$FEED/releases.json" "$TMP/releases.json" || fail "could not get the release list from $FEED (no release is published there yet?), so nothing was installed."
  PICK="$(helper pick "$TMP/releases.json" "$FEED" "$TARGET")" || fail "nothing was installed."
  set -- $PICK
  [ "$#" -eq 4 ] || fail "nothing was installed."
  VERSION="$1"; URL="$2"; SHA="$3"; SIZE="$4"
  say "Downloading baabaa $VERSION…"
  fetch "$URL" "$TMP/baabaa.tar.gz" || fail "the download failed, so nothing was installed."
fi
helper unpack "$TMP/baabaa.tar.gz" "$VERSION" "$SHA" "$SIZE" "$TMP" || fail "nothing was installed."

DIR="$TMP/baabaa-$VERSION"
[ -f "$DIR/bin/baabaa" ] || fail "the release is incomplete, so nothing was installed."
set --
case "$TARGET" in stable|latest) set -- --channel "$TARGET" ;; esac
PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sys; r = sys.argv.pop(1); sys.path[:] = [r] + [p for p in sys.path if p not in ("", ".")]; from baabaa.__main__ import main; main()' "$DIR" install "$@"
exit $?
