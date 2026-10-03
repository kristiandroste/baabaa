"""Reading the public Ollama library (ollama.com) and registry, for "Check for newer models".

The library pages are HTML; these parsers read the model list, a model's tags, and a tag's manifest.
They are deliberately tolerant: anything they cannot read is skipped, never guessed.
"""

import asyncio
import html
import json
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone

LIBRARY_URL = "https://ollama.com/library"
TAGS_URL = "https://ollama.com/library/{name}/tags"
MANIFEST_URL = "https://registry.ollama.ai/v2/library/{name}/manifests/{tag}"
BLOB_URL = "https://registry.ollama.ai/v2/library/{name}/blobs/{digest}"
USER_AGENT = "baabaa (local model manager)"

_LI = re.compile(r'<li[^>]*>\s*<a href="/library/([A-Za-z0-9._-]+)"(.*?)</li>', re.S)
_DESC = re.compile(r'<p class="max-w-lg[^"]*">(.*?)</p>', re.S)
_BADGE = re.compile(r'<span\s+class="inline-flex items-center rounded-md ([^"]*)"\s*>\s*([^<]+?)\s*</span>')
_UPDATED = re.compile(r'title="([A-Z][a-z]{2} \d{1,2}, \d{4} [^"]+ UTC)"')
_PULLS = re.compile(r'<span\s*>\s*([\d.]+[KMB]?)\s*</span>\s*<span[^>]*>&nbsp;Pulls')
_TAG_A = re.compile(r'<a href="/library/([A-Za-z0-9._-]+):([A-Za-z0-9._-]+)" class="md:hidden(.*?)</a>', re.S)
_SIZE_RE = re.compile(r"^(e)?(\d+(?:\.\d+)?)([bm])$", re.I)


def _get(url: str, timeout: float = 30.0, accept: str = "text/html") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    from .util import ssl_context
    ctx = ssl_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read(8 * 2**20)


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def size_to_b(label: str) -> float | None:
    """'9b' -> 9.0, '0.8b' -> 0.8, '300m' -> 0.3, 'e4b' -> 4.0 (effective size). Else None."""
    m = _SIZE_RE.match(label.strip())
    if not m:
        return None
    value = float(m.group(2))
    return value / 1000 if m.group(3).lower() == "m" else value


def parse_library(page: str) -> list[dict]:
    models = []
    for name, body in _LI.findall(page):
        desc = _DESC.search(body)
        caps, sizes, cloud = [], [], False
        for cls, label in _BADGE.findall(body):
            label = label.strip()
            if "indigo" in cls:
                caps.append(label)
            elif "cyan" in cls:
                cloud = cloud or label == "cloud"
            elif "blue" in cls or "ddf4ff" in cls:
                sizes.append(label)
        updated = _UPDATED.search(body)
        pulls = _PULLS.search(body)
        models.append({
            "name": name,
            "description": _text(desc.group(1)) if desc else "",
            "capabilities": caps,
            "sizes": sizes,
            "cloud": cloud,
            "pulls": pulls.group(1) if pulls else None,
            "updated": _parse_date(updated.group(1)) if updated else None,
        })
    return models


def parse_tags(page: str) -> list[dict]:
    """Tags of one model: [{'name', 'tag', 'digest', 'size_gb', 'context', 'inputs', 'age', 'latest'}]."""
    out, seen = [], set()
    for name, tag, body in _TAG_A.findall(page):
        if tag in seen:
            continue
        seen.add(tag)
        text = _text(body)
        digest = re.search(r"\b([0-9a-f]{12})\b", text)
        size = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)\b", text)
        ctx = re.search(r"(\d+(?:\.\d+)?[KM]?)\s+context window", text)
        inputs = re.search(r"((?:Text|Image|Audio|Video)(?:,\s*(?:Text|Image|Audio|Video))*)\s+input", text)
        age = re.search(r"(\d+\s+\w+\s+ago|yesterday|today)", text)
        out.append({
            "name": name,
            "tag": tag,
            "digest": digest.group(1) if digest else None,
            "size_gb": (float(size.group(1)) / (1 if size.group(2) == "GB" else 1024)) if size else None,
            "context": _ctx(ctx.group(1)) if ctx else None,
            "inputs": inputs.group(1) if inputs else None,
            "age": age.group(1) if age else None,
            "latest": bool(re.search(r"\blatest\b", text)) and tag != "latest",
        })
    return out


def _ctx(label: str) -> int | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KM]?)", label.strip(), re.I)
    if not m:
        return None
    mult = {"": 1, "K": 1024, "M": 1024 * 1024}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def _parse_date(text: str) -> str | None:
    try:
        dt = datetime.strptime(text.replace(" UTC", ""), "%b %d, %Y %I:%M %p").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


async def fetch_library() -> list[dict]:
    page = await asyncio.to_thread(_get, LIBRARY_URL)
    return parse_library(page.decode("utf-8", "replace"))


async def fetch_tags(name: str) -> list[dict]:
    page = await asyncio.to_thread(_get, TAGS_URL.format(name=name))
    return parse_tags(page.decode("utf-8", "replace"))


async def fetch_registry_info(name: str, tag: str) -> dict:
    """From the registry: the weights' exact size (`weights`) and the oldest Ollama that can run the model
    (`requires`, from its config; None when it names none). Values that cannot be read are None."""
    out = {"weights": None, "requires": None}
    try:
        raw = await asyncio.to_thread(
            _get, MANIFEST_URL.format(name=name, tag=tag), 30.0,
            "application/vnd.docker.distribution.manifest.v2+json")
        manifest = json.loads(raw)
    except (urllib.error.URLError, OSError, ValueError):
        return out
    total = 0
    for layer in manifest.get("layers", []):
        if layer.get("mediaType") in ("application/vnd.ollama.image.model", "application/vnd.ollama.image.projector"):
            total += int(layer.get("size") or 0)
    out["weights"] = total or None
    digest = (manifest.get("config") or {}).get("digest")
    if digest:
        try:
            config = json.loads(await asyncio.to_thread(_get, BLOB_URL.format(name=name, digest=digest), 30.0, "application/json"))
            out["requires"] = config.get("requires") or None
        except (urllib.error.URLError, OSError, ValueError):
            pass
    return out


async def fetch_weights_bytes(name: str, tag: str) -> int | None:
    """Exact size of the weights layer from the registry manifest."""
    return (await fetch_registry_info(name, tag))["weights"]


def version_tuple(text: str | None) -> tuple:
    """'0.30.11' -> (0, 30, 11); anything unreadable -> ()."""
    nums = re.findall(r"\d+", (text or "").split("-")[0])
    return tuple(int(n) for n in nums[:3])


def needs_newer(requires: str | None, have: str | None) -> bool:
    """True when a model needs a newer Ollama than the one installed (False when either is unknown)."""
    need, got = version_tuple(requires), version_tuple(have)
    return bool(need and got and need > got)
