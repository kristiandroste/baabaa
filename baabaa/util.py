"""Small shared helpers: ids, time, JSON."""

import json
import os
import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def mono_ms() -> float:
    return time.monotonic_ns() / 1_000_000


def new_id() -> str:
    """26-character, time-sortable id (48-bit milliseconds + 80 random bits, Crockford base32)."""
    value = (now_ms() << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out)).lower()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(text, default=None):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def extract_json(text: str):
    """The first JSON object in `text`, tolerating Markdown fences and chatter around it. None if absent."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    try:
        return json.loads(s)
    except ValueError:
        pass
    start = s.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except ValueError:
                        break
        start = s.find("{", start + 1)
    return None


_SSL = None


def ssl_context():
    """The TLS settings for baabaa's own HTTPS requests (releases, the model library, web pages, connectors).
    Python from python.org on macOS ships without root certificates until its "Install Certificates" step
    runs; then the system's roots are read from the keychain, so HTTPS works either way."""
    global _SSL
    if _SSL is None:
        import ssl
        import subprocess
        import sys
        ctx = ssl.create_default_context()
        paths = ssl.get_default_verify_paths()
        has_roots = (paths.cafile and os.path.exists(paths.cafile)) or (paths.capath and os.path.isdir(paths.capath)
                                                                         and os.listdir(paths.capath))
        if sys.platform == "darwin" and not has_roots:
            try:
                pem = subprocess.run(["/usr/bin/security", "find-certificate", "-a", "-p",
                                      "/System/Library/Keychains/SystemRootCertificates.keychain"],
                                     capture_output=True, text=True, timeout=20).stdout
                if "BEGIN CERTIFICATE" in pem:
                    ctx.load_verify_locations(cadata=pem)
            except (OSError, subprocess.SubprocessError, ssl.SSLError):
                pass
        _SSL = ctx
    return _SSL
