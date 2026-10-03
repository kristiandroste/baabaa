"""Projects' knowledge: files turned into searchable chunks, and the project's part of the prompt.

A project's documents go into the prompt whole while they fit a share of the model's context. Beyond that,
each message retrieves the most relevant excerpts: keyword search (SQLite FTS5) and, when an embedding
model is approved, semantic search, where the query is embedded through Ollama and compared with every
stored chunk on the GPU (cuda.py). The two rankings are merged by reciprocal rank fusion.
"""

import array
import asyncio
import math
import os
import re

from . import docs
from .gateway import GpuSearchError, ModelNotAllowed, ResidencyError
from .ollama import OllamaError
from .util import clip

CHUNK_CHARS = 1400
CHUNK_OVERLAP = 200
EMBED_BATCH = 16
QUIET_S = 30  # indexing waits this long after interactive GPU work, so it doesn't evict a conversation's model
FULL_SHARE = 0.3          # documents go into the prompt whole while they use at most this share of the context
CHARS_PER_TOKEN = 3.3
MAX_FILE_BYTES = 50 * 2**20
QUERY_PREFIX = "task: search result | query: "   # EmbeddingGemma's retrieval prompts


def doc_prefix(title: str) -> str:
    return f"title: {title or 'none'} | text: "


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Chunks of about `size` characters, cut at paragraph, line or sentence ends, each starting with the
    last `overlap` characters of the one before."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    pieces = []  # (text, joiner that goes before it inside a chunk)
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            pieces.append((para, "\n\n"))
            continue
        parts = re.split(r"((?<=[.!?])\s+|\n)", para)
        joiner = "\n\n"
        for i in range(0, len(parts), 2):
            part = parts[i].strip()
            while len(part) > size:
                cut = part.rfind(" ", 0, size)
                cut = cut if cut > size // 2 else size
                pieces.append((part[:cut], joiner))
                part, joiner = part[cut:].lstrip(), " "
            if part:
                pieces.append((part, joiner))
            sep = parts[i + 1] if i + 1 < len(parts) else " "
            joiner = "\n" if "\n" in sep else " "
    chunks, cur = [], ""
    for p, joiner in pieces:
        if cur and len(cur) + len(p) + len(joiner) > size:
            chunks.append(cur)
            tail = cur[-overlap:]
            sp = tail.find(" ")
            cur = (tail[sp + 1:] if 0 <= sp < len(tail) - 1 else tail) + joiner + p
        else:
            cur = (cur + joiner + p) if cur else p
    if cur.strip():
        chunks.append(cur)
    return chunks


def normalize(v: list[float]) -> list[float]:
    s = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / s for x in v]


def to_blob(v: list[float]) -> bytes:
    return array.array("f", normalize(v)).tobytes()


class Knowledge:
    def __init__(self, app):
        self.app = app
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._again: set[tuple[str, str]] = set()

    # files ------------------------------------------------------------------------------------------
    def file_dir(self, account_id: str, project_id: str):
        return self.app.paths.account_files(account_id, "projects", project_id)

    async def add_file(self, account_id: str, project_id: str, name: str, mime: str, data: bytes) -> dict:
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("That file is larger than 50 MB")
        kind = docs.classify(name, mime, data)
        if kind not in ("text", "document"):
            raise ValueError(f"{name}: project knowledge takes text, code, PDF and office documents, not {kind} files")
        text = await asyncio.to_thread(docs.extract, name, data) or ""
        if not text.strip():
            raise ValueError(f"{name}: no text could be read from this file (a scanned document?)")
        store = self.app.stores.get(account_id)
        rec = store.add_project_file(project_id, name, mime, len(data), kind, None, text)
        path = os.path.join(self.file_dir(account_id, project_id), f"{rec['id']}-{name}")
        with open(path, "wb") as f:
            f.write(data)
        store.con.execute("UPDATE project_files SET path=? WHERE id=?", (path, rec["id"]))
        self.schedule(account_id, project_id)
        return store.project_file(rec["id"], with_text=False)

    def add_text(self, account_id: str, project_id: str, name: str, text: str) -> dict:
        if not text.strip():
            raise ValueError("The text is empty")
        store = self.app.stores.get(account_id)
        name = name.strip() or "Text"
        rec = store.add_project_file(project_id, name, "text/plain", len(text.encode()), "text", None, text)
        self.schedule(account_id, project_id)
        return rec

    def delete_file(self, account_id: str, fid: str) -> None:
        store = self.app.stores.get(account_id)
        rec = store.project_file(fid, with_text=False)
        if rec is None:
            raise KeyError(fid)
        store.delete_project_file(fid)
        if rec.get("path"):
            try:
                os.unlink(rec["path"])
            except OSError:
                pass

    def delete_project(self, account_id: str, project_id: str) -> None:
        import shutil
        task = self._tasks.pop((account_id, project_id), None)
        if task:
            task.cancel()
        self.app.stores.get(account_id).delete_project(project_id)
        shutil.rmtree(self.file_dir(account_id, project_id), ignore_errors=True)

    # indexing ----------------------------------------------------------------------------------------
    def schedule(self, account_id: str, project_id: str) -> None:
        key = (account_id, project_id)
        task = self._tasks.get(key)
        if task and not task.done():
            self._again.add(key)  # a file arrived while indexing: go round once more
            return
        self._tasks[key] = asyncio.get_running_loop().create_task(self._index(account_id, project_id))

    def resume(self) -> None:
        """At startup: finish indexing that a restart interrupted, and re-embed for a changed embedding model."""
        model = self.app.registry.embed_model()
        for account in self.app.accounts.list():
            store = self.app.stores.get(account["id"])
            q = "SELECT DISTINCT project_id FROM project_files WHERE status IN ('new', 'indexing')"
            args = ()
            if model:
                q += " OR (status='ready' AND COALESCE(embed_model, '') != ?)"
                args = (model,)
            for (pid,) in store.con.execute(q, args).fetchall():
                self.schedule(account["id"], pid)

    def _publish(self, account_id: str, project_id: str, fid: str, **extra) -> None:
        rec = self.app.stores.get(account_id).project_file(fid, with_text=False)
        if rec:
            rec.pop("path", None)
            self.app.events.publish(account_id, "project.file", {"project_id": project_id, "file": {**rec, **extra}})

    async def _index(self, account_id: str, project_id: str) -> None:
        store = self.app.stores.get(account_id)
        while True:
            model = self.app.registry.embed_model()
            todo = [f for f in store.project_files(project_id)
                    if f["status"] in ("new", "indexing") or (model and f["status"] == "ready" and f["embed_model"] != model)]
            for f in todo:
                await self._index_file(account_id, project_id, f["id"], model)
            key = (account_id, project_id)
            if key not in self._again:
                break
            self._again.discard(key)

    async def _index_file(self, account_id: str, project_id: str, fid: str, model: str | None) -> None:
        store = self.app.stores.get(account_id)
        rec = store.project_file(fid)
        if rec is None:
            return
        chunks = chunk_text(rec["text"])
        store.set_file_status(fid, "indexing")
        store.replace_chunks(fid, project_id, [(c, None) for c in chunks])  # keyword search works from here
        self._publish(account_id, project_id, fid, done=0, total=len(chunks))
        if not model:
            store.set_file_status(fid, "ready", None, None)
            self._publish(account_id, project_id, fid)
            return
        ids = [r[0] for r in store.con.execute("SELECT id FROM chunks WHERE file_id=? ORDER BY seq", (fid,))]
        try:
            for i in range(0, len(chunks), EMBED_BATCH):
                batch = [doc_prefix(rec["name"]) + c for c in chunks[i:i + EMBED_BATCH]]
                await self.app.gateway.queue.quiet(QUIET_S)  # keyword search works meanwhile
                vectors = await self.app.gateway.embed(model, batch, account_id=account_id, window="background")
                with store.lock:
                    for cid, v in zip(ids[i:i + EMBED_BATCH], vectors):
                        store.con.execute("UPDATE chunks SET vector=? WHERE id=?", (to_blob(v), cid))
                self._publish(account_id, project_id, fid, done=min(len(chunks), i + EMBED_BATCH), total=len(chunks))
            store.set_file_status(fid, "ready", None, model)
            self.app.stats.event("knowledge_indexed", account_id, None, "background", project_id=project_id,
                                 chunks=len(chunks), chars=len(rec["text"]), model=model)
        except (OllamaError, ResidencyError, ModelNotAllowed, OSError) as exc:
            store.set_file_status(fid, "ready", f"Semantic index failed ({exc}); keyword search still works.", None)
        self._publish(account_id, project_id, fid)

    # search ------------------------------------------------------------------------------------------
    async def search(self, account_id: str, project_id: str, query: str, k: int = 6, conv_id=None, window=None) -> dict:
        store = self.app.stores.get(account_id)
        keyword = store.chunk_keyword(project_id, query, 30)
        semantic, note = [], None
        model = self.app.registry.embed_model()
        if model:
            if store.con.execute("SELECT 1 FROM project_files WHERE project_id=? AND status='ready'"
                                 " AND COALESCE(embed_model, '') != ? LIMIT 1", (project_id, model)).fetchone():
                self.schedule(account_id, project_id)  # the embedding model changed since these were indexed
            ids, matrix, dim = store.chunk_vectors(project_id, model)
            if ids:
                try:
                    qv = (await self.app.gateway.embed(model, [QUERY_PREFIX + query], account_id=account_id, window=window,
                                                       conv_id=conv_id))[0]
                    top = await self.app.gateway.vector_search(matrix, len(ids), dim, normalize(qv), 30,
                                                               account_id=account_id, conv_id=conv_id, window=window)
                    semantic = [ids[i] for i, _ in top]
                except (GpuSearchError, OllamaError, ResidencyError, ModelNotAllowed) as exc:
                    note = f"Semantic search was unavailable ({clip(str(exc), 160)}), so only keyword search was used."
        else:
            note = "No embedding model is approved, so only keyword search was used."
        scores: dict[int, float] = {}
        for ranking in (semantic, keyword):
            for rank, cid in enumerate(ranking):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (60 + rank)
        best = sorted(scores, key=lambda c: -scores[c])[:k]
        results = store.chunks_by_ids(best)
        self.app.stats.event("knowledge_search", account_id, conv_id, window, project_id=project_id,
                             semantic=len(semantic), keyword=len(keyword), returned=len(results))
        return {"results": results, "method": "hybrid" if semantic else "keyword", "note": note}

    @staticmethod
    def format_results(res: dict) -> str:
        if not res["results"]:
            return "Nothing in the project's documents matches." + (f" ({res['note']})" if res.get("note") else "")
        parts = [f"[{i}] {r['name']} (part {r['seq'] + 1})\n{r['text']}" for i, r in enumerate(res["results"], 1)]
        tail = f"\n\n({res['note']})" if res.get("note") else ""
        return "Excerpts from the project's documents:\n\n" + "\n\n".join(parts) + tail

    # the prompt -------------------------------------------------------------------------------------
    def prompt(self, account_id: str, project: dict, num_ctx: int) -> tuple[str, str]:
        """The project's section of the system prompt, and the knowledge mode: 'none', 'full' or 'search'."""
        store = self.app.stores.get(account_id)
        parts = [f"# Project: {project['name']}"]
        if project["description"].strip():
            parts.append(project["description"].strip()[:1000])
        if project["instructions"].strip():
            parts.append("\n## Project instructions (follow them)\n" + project["instructions"].strip()[:8000])
        files = store.project_files(project["id"])
        if not files:
            return "\n".join(parts), "none"
        total = sum(f["chars"] for f in files)
        if total <= int(num_ctx * FULL_SHARE * CHARS_PER_TOKEN):
            docs_ = []
            for f in files:
                rec = store.project_file(f["id"])
                docs_.append(f'<document name="{rec["name"]}">\n{rec["text"]}\n</document>')
            parts.append("\n## Project knowledge\nThe user added these documents to the project. Use them when they are "
                         "relevant and say which document you used.\n\n" + "\n\n".join(docs_))
            return "\n".join(parts), "full"
        names = ", ".join(f["name"] for f in files[:30]) + (" …" if len(files) > 30 else "")
        parts.append(f"\n## Project knowledge\nThe project has {len(files)} documents ({names}), too large to include "
                     "whole. Excerpts relevant to each message are searched for you automatically; call search_project "
                     "to look for something else. Say which document an answer comes from.")
        return "\n".join(parts), "search"
