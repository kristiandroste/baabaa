"""Finding models to install: newer versions of the installed ones, and searches by need.

Newer versions (of every installed model, or of one):
1. Read the installed models and the Ollama library (names, sizes, capabilities, update dates).
2. For each installed model, keep library models of the same role and a similar parameter size,
   excluding sizes whose weights could never fit in this GPU.
3. Ask the local model to pick the true successors and say why (strict JSON). Without an approved
   chat model, a name-based heuristic is used instead and the result says so.
4. For each pick, read the exact tag, download size and context window from the library, and the
   weight size from the registry manifest; drop anything whose weights cannot fit in VRAM.
5. Also flag installed official models whose tag now points to a newer build (digest changed).

Search (free text, or one of the CATEGORIES): library models that match and could fit, newest and most
used first, from which the local model picks up to five; each pick is then checked like step 4.

Suggestions the owner dismissed are never shown again (a newer build only until the next one).
"""

import asyncio
import json
import re
from datetime import datetime, timezone

from . import library
from .gateway import ModelNotAllowed
from .ollama import OllamaError
from .util import clip, extract_json

MAX_CANDIDATES = 8
SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "installed": {"type": "string"},
                    "candidate": {"type": "string"},
                    "size": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["installed", "candidate", "size", "reason"],
            },
        }
    },
    "required": ["recommendations"],
}

PROMPT = """You help keep a small local model collection up to date.

Installed models (name, role, parameter size in billions, capabilities, install date):
{installed}

Candidate models from the Ollama library, per installed model. Each line gives the library name,
its available sizes, capabilities, last update date and description:
{candidates}

For each installed model, pick at most one candidate that are a newer version, a newer generation or
a clear successor of the installed model, in a size close to the installed one. Prefer the same
family or maker (for example qwen3.5 -> qwen3.6 or qwen4; gemma3 -> gemma4). Skip a candidate if it is
older than the installed model, if it is a different kind of model (for example a coder or vision-only
model replacing a general model), or if no listed size is close to the installed size. Returning no
recommendations is fine.

Answer with JSON only: {{"recommendations": [{{"installed": "<installed name>", "candidate": "<library name>",
"size": "<one of the candidate's sizes>", "reason": "<one sentence>"}}]}}"""


# id: (label, what the person is looking for, as the search prompt says it)
CATEGORIES = {
    "general": ("General chat", "a general-purpose assistant for everyday questions, writing and analysis"),
    "coding": ("Coding", "a model made or tuned for writing, reading and fixing code"),
    "vision": ("Vision", "a model that understands images: photos, screenshots and scanned pages"),
    "reasoning": ("Reasoning", "a model that thinks step by step before answering, for math and hard problems"),
    "agents": ("Agents and tools", "a model that calls tools reliably, for agent and coding work"),
    "small": ("Small and fast", "a small model that answers quickly"),
    "embedding": ("Embeddings", "an embedding model for searching files and knowledge"),
}
SEARCH_PICKS = 5
SEARCH_CANDIDATES = 20
SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"candidate": {"type": "string"}, "size": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["candidate", "size", "reason"],
            },
        }
    },
    "required": ["recommendations"],
}
SEARCH_PROMPT = """You help someone choose a local model to install. Every model must fit in their GPU's memory
({vram}); the sizes listed below are the ones that can.

They are looking for: {need}

Models they already have: {installed}

Candidates from the Ollama library (name | sizes that fit | capabilities | last updated | pulls | description):
{candidates}

Pick up to {picks} candidates that best match what they are looking for. Prefer newer and stronger models,
and the largest listed size. Skip candidates that do not match the request, and models they already have
in the same size. Returning fewer is fine.

Answer with JSON only: {{"recommendations": [{{"candidate": "<library name>", "size": "<one of its sizes>",
"reason": "<one sentence on why it suits the request>"}}]}}"""

# words of a free-text search that say nothing about the model, and words that point at a capability
_STOP = set("a an the best good great top new newer newest latest recent model models llm for with and or that "
            "to of me my i want need find which what is are in on local ollama gpu fits fit one some run can".split())
_SYNONYMS = {"image": "vision", "images": "vision", "picture": "vision", "pictures": "vision", "photo": "vision",
             "photos": "vision", "screenshot": "vision", "screenshots": "vision", "ocr": "vision", "coding": "code",
             "coder": "code", "programming": "code", "think": "thinking", "reasoning": "thinking", "math": "thinking",
             "agent": "tools", "agents": "tools", "tool": "tools", "embed": "embedding", "embeddings": "embedding",
             "rag": "embedding"}
BYTES_PER_B = 0.62e9  # a 4-bit build's weights per billion parameters, to rule sizes in or out before the exact check


def rec_key(rec: dict) -> str:
    """How a dismissed suggestion is remembered: the tag, or for a newer build the tag and its digest."""
    return f"{rec['model']}@{rec.get('digest') or ''}" if rec.get("kind") == "rebuild" else rec["model"]


def _base(name: str) -> str:
    return name.split(":", 1)[0]


def _family_key(name: str) -> str:
    """'qwen3.5' -> 'qwen', 'gemma4' -> 'gemma', 'llama3.1' -> 'llama'."""
    return re.sub(r"[\d._-].*$", "", _base(name).split("/")[-1].lower()) or _base(name)


def _version(name: str) -> tuple:
    nums = re.findall(r"\d+(?:\.\d+)?", _base(name).split("/")[-1])
    return tuple(float(n) for n in nums[:1]) if nums else ()


def _pulls(text: str | None) -> float:
    if not text:
        return 0.0
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(text[-1:].upper(), 1)
    try:
        return float(text.rstrip("KMBkmb")) * mult
    except ValueError:
        return 0.0


def _date(text: str | None):
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _weight_limit(gpu) -> int:
    mem = gpu.memory()
    vram = mem["total"] if mem else 8 * 2**30
    return vram - 1 * 2**30  # weights must leave room for at least a small KV cache


async def _ollama_version(jobs) -> str | None:
    try:
        return await jobs.ollama.version()
    except (AttributeError, OllamaError, OSError):
        return None


async def _details(name: str, size: str, entry: dict, weight_limit: int, have: str | None = None) -> dict | None:
    """Download size, context window and inputs of `name:size` from the library and registry; None when its
    weights cannot fit in VRAM (or their size cannot be read). `requires` is set when the model needs a
    newer Ollama than `have`, the installed one."""
    try:
        tags = await library.fetch_tags(name)
    except OSError:
        tags = []
    info = next((t for t in tags if t["tag"] == size), {})
    reg = await library.fetch_registry_info(name, size)
    weights = reg["weights"]
    if weights is None and info.get("size_gb"):
        weights = int(info["size_gb"] * 1e9)
    if weights is None or weights > weight_limit:
        return None
    return {"download_bytes": weights, "context": info.get("context"), "inputs": info.get("inputs"),
            "requires": reg["requires"] if library.needs_newer(reg["requires"], have) else None, "ollama": have,
            "updated": entry.get("updated"), "description": entry.get("description"), "capabilities": entry.get("capabilities"),
            "pulls": entry.get("pulls")}


async def scout(jobs, job_id: str, account_id, other_families: bool = False, only: str | None = None) -> dict:
    """Newer versions of the installed models, or of the one named `only`."""
    registry = jobs.registry
    weight_limit = _weight_limit(jobs.gpu)
    dismissed = set(jobs.maindb.get_setting("scout_dismissed") or [])

    jobs._progress(job_id, {"phase": "installed"})
    await registry.sync()

    jobs._progress(job_id, {"phase": "library"})
    lib = await library.fetch_library()
    lib = [x for x in lib if x["sizes"]]
    lib_by_name = {x["name"]: x for x in lib}

    # Installed models that come from the official library (custom and namespaced models have no
    # lineage to follow), fitting the GPU, one per base name and size.
    installed, seen_keys = [], set()
    for m in registry.all():
        base = _base(m["name"])
        if only and m["name"] != only:
            continue
        if "/" in m["name"] or base not in lib_by_name or not m["param_b"]:
            continue
        if (m["info"].get("size") or 0) > weight_limit:
            continue
        key = (base, round(m["param_b"], 1))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        installed.append(m)
    scope = {"scope": "model" if only else "all", "model": only, "other_families": other_families, "library_models": len(lib)}
    if only and not installed:
        return {**scope, "recommendations": [], "checked": [],
                "note": f"baabaa follows versions only of models from the Ollama library; {only} is not one."}

    # candidates per installed model -------------------------------------------------------------
    per_model = {}
    for m in installed:
        p = m["param_b"]
        is_embed = m["role"] == "embed"
        released = _date(lib_by_name[_base(m["name"])].get("updated"))
        fam = _family_key(m["name"])
        cands = []
        for x in lib:
            if _base(x["name"]) == _base(m["name"]):
                continue
            if is_embed != ("embedding" in x["capabilities"]):
                continue
            close = [s for s in x["sizes"] if (b := library.size_to_b(s)) and 0.7 * p <= b <= 1.35 * p]
            if not close:
                continue
            updated = _date(x.get("updated"))
            if released and updated and updated <= released:
                continue  # not newer than the installed model's own library entry
            if not other_families and _family_key(x["name"]) != fam:
                continue  # an update means the same family unless the owner asks for others
            cands.append({**x, "close_sizes": close})
        # same family first, then the most used
        cands.sort(key=lambda x: (_family_key(x["name"]) != fam, -_pulls(x.get("pulls"))))
        per_model[m["name"]] = cands[:MAX_CANDIDATES]

    # ask the local model ------------------------------------------------------------------------
    jobs._progress(job_id, {"phase": "thinking"})
    picks, method = [], "model"
    judge = registry.default_chat()
    if judge and any(per_model.values()):
        inst_lines = "\n".join(
            f"- {m['name']} | {m['role']} | {m['param_b']:g}B | {', '.join(m['info'].get('capabilities') or [])} | "
            f"{(m['info'].get('modified_at') or '')[:10]}" for m in installed if per_model.get(m["name"]))
        cand_lines = []
        for name, cands in per_model.items():
            if not cands:
                continue
            cand_lines.append(f"For {name}:")
            for x in cands:
                cand_lines.append(
                    f"  - {x['name']} | sizes {', '.join(x['close_sizes'])} | {', '.join(x['capabilities'])} | "
                    f"{(x.get('updated') or '')[:10]} | {clip(x['description'], 140)}")
        prompt = PROMPT.format(installed=inst_lines, candidates="\n".join(cand_lines))
        try:
            caps = (registry.get(judge)["info"].get("capabilities") or [])
            res = await jobs.gateway.complete(
                model=judge, kind="scout", messages=[{"role": "user", "content": prompt}], fmt=SCHEMA,
                think=False if "thinking" in caps else None, options={"temperature": 0},
                account_id=account_id, label="checking for newer models")
            data = extract_json(res["content"])
            if not isinstance(data, dict):
                raise ValueError("the model did not return JSON")
            picks = [r for r in data.get("recommendations", []) if isinstance(r, dict)]
        except (OllamaError, ModelNotAllowed, ValueError, KeyError) as exc:
            method = f"heuristic (model step failed: {clip(str(exc), 120)})"
            picks = []
    else:
        method = "heuristic (no approved chat model yet)"
    # The name-based lineage check always runs too: a small model can miss an obvious successor.
    chosen = {(p.get("installed"), p.get("candidate")) for p in picks}
    for p in _heuristic(installed, per_model):
        if (p["installed"], p["candidate"]) not in chosen:
            picks.append(p)

    # verify each pick against the library and the GPU ------------------------------------------
    jobs._progress(job_id, {"phase": "verifying"})
    have = await _ollama_version(jobs)
    lib_names = {x["name"]: x for x in lib}
    installed_names = {m["name"] for m in registry.all()}
    recs, seen = [], set()
    for p in picks:
        cand, inst, size = p.get("candidate", "").strip(), p.get("installed", "").strip(), p.get("size", "").strip().lower()
        if cand not in lib_names or inst not in per_model:
            continue
        if size not in [s.lower() for s in lib_names[cand]["sizes"]]:
            close = [c for c in per_model[inst] if c["name"] == cand]
            if not close:
                continue
            size = close[0]["close_sizes"][0]
        tag = f"{cand}:{size}"
        if tag in seen or tag in installed_names or tag in dismissed:
            continue
        seen.add(tag)
        details = await _details(cand, size, lib_names[cand], weight_limit, have)
        if details:
            recs.append({"model": tag, "replaces": inst, "reason": clip(p.get("reason", ""), 300), "kind": "successor", **details})

    # newer builds of the installed tags ---------------------------------------------------------
    for m in installed:
        name = m["name"]
        if "/" in name:
            continue
        base, tag = (name.split(":", 1) + ["latest"])[:2]
        if base not in lib_names:
            continue
        try:
            tags = await library.fetch_tags(base)
        except OSError:
            continue
        info = next((t for t in tags if t["tag"] == tag), None)
        if info and info.get("digest") and m.get("digest") and not m["digest"].startswith(info["digest"]) \
                and f"{name}@{info['digest']}" not in dismissed:
            recs.append({"model": name, "replaces": name, "kind": "rebuild", "digest": info["digest"],
                         "download_bytes": int((info.get("size_gb") or 0) * 1e9),
                         "reason": "The library has a newer build of this exact tag.", "context": info.get("context"),
                         "inputs": info.get("inputs"), "updated": lib_names[base].get("updated"),
                         "description": lib_names[base].get("description"), "capabilities": lib_names[base].get("capabilities")})
        await asyncio.sleep(0)

    return {**scope, "recommendations": recs, "method": method, "judge": judge if method == "model" else None,
            "checked": [m["name"] for m in installed]}


def _search_words(query: str) -> list[str]:
    words = []
    for w in re.findall(r"[a-z0-9][a-z0-9.+#-]*", query.lower()):
        if w not in _STOP:
            words.append(_SYNONYMS.get(w, w))
    return list(dict.fromkeys(words))


def _matches_category(x: dict, category: str | None) -> bool:
    caps = x["capabilities"]
    if category == "embedding":
        return "embedding" in caps
    if "embedding" in caps:
        return False
    if category == "vision":
        return "vision" in caps
    if category == "reasoning":
        return "thinking" in caps
    if category == "agents":
        return "tools" in caps
    if category == "coding":  # "qwen2.5-coder", "codestral", "devstral"; "code" in a description, not "encoder"
        return bool(re.search(r"cod(e|er|ing)|devstral", x["name"].lower())
                    or re.search(r"\b(code|coder|coding|programming)\b", x["description"].lower()))
    return True


def _fitting_sizes(x: dict, weight_limit: int, max_b: float | None = None) -> list[str]:
    """The sizes of a library model whose weights could fit, smallest first (the exact check comes later)."""
    out = []
    for size in x["sizes"]:
        b = library.size_to_b(size)
        if not b:
            continue
        params = b * (2 if size.lower().startswith("e") else 1)  # "e4b" counts effective parameters; the file holds more
        if (not max_b or params <= max_b) and params * BYTES_PER_B <= weight_limit * 1.1:
            out.append(size)
    return sorted(out, key=lambda z: library.size_to_b(z) or 0)


async def search(jobs, job_id: str, account_id, query: str = "", category: str | None = None) -> dict:
    """Library models that suit a free-text request or a category and fit this GPU."""
    registry = jobs.registry
    weight_limit = _weight_limit(jobs.gpu)
    dismissed = set(jobs.maindb.get_setting("scout_dismissed") or [])
    query = query.strip()
    need = query or CATEGORIES[category][1]
    scope = {"scope": "search", "query": query or None, "category": category}

    jobs._progress(job_id, {"phase": "library"})
    await registry.sync()
    lib = [x for x in await library.fetch_library() if x["sizes"]]
    words = _search_words(query)
    kind = "embedding" if category == "embedding" or "embedding" in words else category
    cands = []
    for x in lib:
        if not _matches_category(x, kind):
            continue
        sizes = _fitting_sizes(x, weight_limit, 4 if category == "small" else None)
        if not sizes:
            continue
        text = f"{x['name']} {x['description']} {' '.join(x['capabilities'])}".lower()
        cands.append({**x, "fit_sizes": sizes, "score": sum(1 for w in words if w in text)})
    if any(c["score"] for c in cands):
        cands = [c for c in cands if c["score"]]

    def stamp(c):
        d = _date(c.get("updated"))
        return d.timestamp() if d else 0.0

    # the best matches first; among equals, alternately the newest and the most used
    newest = sorted(cands, key=lambda c: (-c["score"], -stamp(c)))
    popular = sorted(cands, key=lambda c: (-c["score"], -_pulls(c.get("pulls"))))
    shortlist, seen = [], set()
    for pair in zip(newest, popular):
        for c in pair:
            if c["name"] not in seen and len(shortlist) < SEARCH_CANDIDATES:
                seen.add(c["name"])
                shortlist.append(c)

    jobs._progress(job_id, {"phase": "thinking"})
    picks, method = [], "model"
    judge = registry.default_chat()
    installed_names = {m["name"] for m in registry.all()}
    if judge and shortlist:
        lines = [f"- {c['name']} | {', '.join(c['fit_sizes'])} | {', '.join(c['capabilities']) or 'text'} | "
                 f"{(c.get('updated') or '')[:10]} | {c.get('pulls') or '?'} | {clip(c['description'], 140)}" for c in shortlist]
        prompt = SEARCH_PROMPT.format(vram=f"{(weight_limit + 2**30) / 2**30:.0f} GiB", need=need,
                                      installed=", ".join(sorted(installed_names)) or "none",
                                      candidates="\n".join(lines), picks=SEARCH_PICKS)
        try:
            caps = (registry.get(judge)["info"].get("capabilities") or [])
            res = await jobs.gateway.complete(
                model=judge, kind="scout", messages=[{"role": "user", "content": prompt}], fmt=SEARCH_SCHEMA,
                think=False if "thinking" in caps else None, options={"temperature": 0},
                account_id=account_id, label="searching for models")
            data = extract_json(res["content"])
            if not isinstance(data, dict):
                raise ValueError("the model did not return JSON")
            picks = [r for r in data.get("recommendations", []) if isinstance(r, dict)][:SEARCH_PICKS]
        except (OllamaError, ModelNotAllowed, ValueError, KeyError) as exc:
            method = f"heuristic (model step failed: {clip(str(exc), 120)})"
    else:
        method = "heuristic (no approved chat model yet)"
    if method != "model":
        picks = [{"candidate": c["name"], "size": c["fit_sizes"][-1],
                  "reason": "Matches your search." if c["score"] else "Among the newest and most used that fit."}
                 for c in shortlist[:SEARCH_PICKS]]

    # check each pick against the library and the GPU; if its size cannot fit, try the next smaller one
    jobs._progress(job_id, {"phase": "verifying"})
    have = await _ollama_version(jobs)
    by_name = {c["name"]: c for c in cands}
    recs, tried = [], set()
    for p in picks:
        c = by_name.get(str(p.get("candidate", "")).strip())
        if not c:
            continue
        size = str(p.get("size", "")).strip().lower()
        sizes = [z for z in c["fit_sizes"] if z.lower() == size] or c["fit_sizes"][-1:]
        smaller = [z for z in c["fit_sizes"] if (library.size_to_b(z) or 0) < (library.size_to_b(sizes[0]) or 0)]
        for size in sizes + smaller[-1:]:
            tag = f"{c['name']}:{size}"
            if tag in tried or tag in dismissed:
                break
            tried.add(tag)
            details = await _details(c["name"], size, c, weight_limit, have)
            if details:
                recs.append({"model": tag, "kind": "match", "reason": clip(p.get("reason", ""), 300), **details})
                break
    return {**scope, "recommendations": recs, "method": method, "judge": judge if method == "model" else None,
            "library_models": len(lib), "candidates": len(cands)}


def _heuristic(installed, per_model) -> list[dict]:
    picks = []
    for m in installed:
        fam, ver = _family_key(m["name"]), _version(m["name"])
        for x in per_model.get(m["name"], []):
            if _family_key(x["name"]) == fam and _version(x["name"]) > ver:
                picks.append({"installed": m["name"], "candidate": x["name"], "size": x["close_sizes"][0],
                              "reason": f"A newer {fam} release in a similar size."})
                break
    return picks
