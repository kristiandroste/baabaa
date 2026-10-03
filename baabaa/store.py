"""Per-account storage: conversations as message trees, search, attachments, artifacts, rules, projects
with their knowledge chunks, and memory.

Each account has its own directory and SQLite database. A conversation is a tree of messages; editing
or regenerating adds a sibling, and `leaf_id` marks the branch on screen. One assistant message holds
a whole turn: its thinking, text and tool calls with their results, in order.
"""

import threading

from . import db
from .util import dumps, loads, new_id, now_ms

MIGRATIONS = [
    """
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL,
        model TEXT,
        mode TEXT NOT NULL DEFAULT 'manual',
        folder TEXT,
        pinned INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        incognito INTEGER NOT NULL DEFAULT 0,
        leaf_id TEXT,
        settings TEXT NOT NULL DEFAULT '{}',
        project_id TEXT
    );
    CREATE INDEX conv_updated ON conversations(updated_ms);
    CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        parent_id TEXT,
        role TEXT NOT NULL,
        blocks TEXT NOT NULL DEFAULT '[]',
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL,
        model TEXT,
        status TEXT NOT NULL DEFAULT 'ok',
        meta TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX msg_conv ON messages(conv_id, created_ms);
    CREATE INDEX msg_parent ON messages(parent_id);
    CREATE VIRTUAL TABLE search USING fts5(text, conv_id UNINDEXED, msg_id UNINDEXED, tokenize='unicode61 remove_diacritics 2');
    CREATE TABLE attachments (
        id TEXT PRIMARY KEY,
        conv_id TEXT,
        name TEXT NOT NULL,
        mime TEXT NOT NULL,
        size INTEGER NOT NULL,
        kind TEXT NOT NULL,
        path TEXT NOT NULL,
        text TEXT,
        meta TEXT NOT NULL DEFAULT '{}',
        created_ms INTEGER NOT NULL
    );
    CREATE TABLE artifacts (
        id TEXT PRIMARY KEY,
        conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        msg_id TEXT,
        title TEXT NOT NULL,
        kind TEXT NOT NULL,
        language TEXT,
        version INTEGER NOT NULL DEFAULT 1,
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL
    );
    CREATE TABLE artifact_versions (
        artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        content TEXT NOT NULL,
        msg_id TEXT,
        created_ms INTEGER NOT NULL,
        PRIMARY KEY (artifact_id, version)
    );
    CREATE TABLE rules (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('allow', 'ask', 'deny')),
        pattern TEXT NOT NULL,
        created_ms INTEGER NOT NULL
    );
    CREATE TABLE auto_rules (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('trust', 'block', 'exception')),
        text TEXT NOT NULL,
        created_ms INTEGER NOT NULL
    );
    CREATE TABLE checkpoints (
        id TEXT PRIMARY KEY,
        conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        msg_id TEXT NOT NULL,
        folder TEXT NOT NULL,
        manifest TEXT NOT NULL,
        created_ms INTEGER NOT NULL
    );
    CREATE INDEX ckpt_conv ON checkpoints(conv_id, created_ms);
    """,
    """
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        instructions TEXT NOT NULL DEFAULT '',
        settings TEXT NOT NULL DEFAULT '{}',
        archived INTEGER NOT NULL DEFAULT 0,
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL
    );
    CREATE TABLE project_files (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        mime TEXT NOT NULL,
        size INTEGER NOT NULL,
        kind TEXT NOT NULL,
        path TEXT,
        text TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'new',
        error TEXT,
        embed_model TEXT,
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL
    );
    CREATE INDEX pfiles_project ON project_files(project_id, created_ms);
    CREATE TABLE chunks (
        id INTEGER PRIMARY KEY,
        file_id TEXT NOT NULL REFERENCES project_files(id) ON DELETE CASCADE,
        project_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        text TEXT NOT NULL,
        vector BLOB
    );
    CREATE INDEX chunks_file ON chunks(file_id, seq);
    CREATE INDEX chunks_project ON chunks(project_id);
    CREATE VIRTUAL TABLE chunk_search USING fts5(text, chunk_id UNINDEXED, project_id UNINDEXED,
        tokenize='unicode61 remove_diacritics 2');
    CREATE TABLE memories (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        text TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'user',
        conv_id TEXT,
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL
    );
    CREATE INDEX conv_project ON conversations(project_id, updated_ms);
    """,
    """
    CREATE TABLE schedules (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        prompt TEXT NOT NULL,
        spec TEXT NOT NULL,
        settings TEXT NOT NULL DEFAULT '{}',
        enabled INTEGER NOT NULL DEFAULT 1,
        next_ms INTEGER,
        last_ms INTEGER,
        last_conv TEXT,
        last_status TEXT,
        runs INTEGER NOT NULL DEFAULT 0,
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL
    );
    CREATE INDEX schedules_next ON schedules(enabled, next_ms);
    """,
]


def block_text(blocks: list) -> str:
    """Plain text of a message for search and titles (text blocks only)."""
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")


class AccountStore:
    def __init__(self, path, account_id: str):
        self.account_id = account_id
        self.con = db.connect(path)
        db.migrate(self.con, MIGRATIONS)
        self.lock = threading.RLock()

    # account-level key/value settings ------------------------------------------------------------
    def get_setting(self, key: str, default=None):
        r = self.con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_setting(self, key: str, value: str) -> None:
        self.con.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, value))

    # conversations ------------------------------------------------------------------------------
    def create_conversation(self, title="", model=None, mode="manual", folder=None, incognito=False,
                            settings=None, project_id=None) -> dict:
        t = now_ms()
        cid = new_id()
        self.con.execute(
            "INSERT INTO conversations(id, title, created_ms, updated_ms, model, mode, folder, incognito, settings, project_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, title, t, t, model, mode, folder, int(bool(incognito)), dumps(settings or {}), project_id))
        return self.conversation(cid)

    def conversation(self, cid: str) -> dict | None:
        r = db.row(self.con.execute("SELECT * FROM conversations WHERE id=?", (cid,)))
        return _conv(r) if r else None

    def conversations(self, limit=50, before_ms=None, query=None, archived=False, folder_only=False,
                      project_id=None) -> list[dict]:
        where, args = ["incognito=0", "archived=?"], [int(archived)]
        if project_id:
            where.append("project_id=?")
            args.append(project_id)
        if before_ms:
            where.append("updated_ms < ?")
            args.append(before_ms)
        if folder_only:
            where.append("folder IS NOT NULL")
        if query:
            ids = [r[0] for r in self.con.execute(
                "SELECT DISTINCT conv_id FROM search WHERE search MATCH ? LIMIT 500", (_fts_query(query),))]
            title_ids = [r[0] for r in self.con.execute(
                "SELECT id FROM conversations WHERE title LIKE ? LIMIT 200", (f"%{query}%",))]
            ids = list(dict.fromkeys(title_ids + ids))
            if not ids:
                return []
            where.append(f"id IN ({','.join('?' for _ in ids)})")
            args += ids
        q = f"SELECT * FROM conversations WHERE {' AND '.join(where)} ORDER BY pinned DESC, updated_ms DESC LIMIT ?"
        return [_conv(r) for r in db.rows(self.con.execute(q, args + [limit]))]

    def update_conversation(self, cid: str, **fields) -> dict:
        allowed = {"title", "model", "mode", "folder", "pinned", "archived", "leaf_id", "settings", "project_id", "incognito"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"cannot update {k}")
            sets.append(f"{k}=?")
            args.append(dumps(v) if k == "settings" else (int(v) if isinstance(v, bool) else v))
        sets.append("updated_ms=?")
        args += [now_ms(), cid]
        self.con.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id=?", args)
        return self.conversation(cid)

    def patch_settings(self, cid: str, patch: dict) -> dict:
        with self.lock:
            conv = self.conversation(cid)
            settings = {**conv["settings"], **patch}
            return self.update_conversation(cid, settings={k: v for k, v in settings.items() if v is not None})

    def touch(self, cid: str) -> None:
        self.con.execute("UPDATE conversations SET updated_ms=? WHERE id=?", (now_ms(), cid))

    def delete_conversation(self, cid: str) -> None:
        with db.Tx(self.con):
            self.con.execute("DELETE FROM search WHERE conv_id=?", (cid,))
            self.con.execute("DELETE FROM conversations WHERE id=?", (cid,))

    def mark_interrupted(self) -> int:
        """Replies left streaming by a server restart are marked stopped."""
        cur = self.con.execute("UPDATE messages SET status='stopped' WHERE status='streaming'")
        return cur.rowcount

    def purge_incognito(self) -> int:
        ids = [r[0] for r in self.con.execute("SELECT id FROM conversations WHERE incognito=1")]
        for cid in ids:
            self.delete_conversation(cid)
        return len(ids)

    # messages -----------------------------------------------------------------------------------
    def add_message(self, cid: str, parent_id, role: str, blocks: list, model=None, status="ok", meta=None) -> dict:
        t = now_ms()
        mid = new_id()
        self.con.execute(
            "INSERT INTO messages(id, conv_id, parent_id, role, blocks, created_ms, updated_ms, model, status, meta)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mid, cid, parent_id, role, dumps(blocks), t, t, model, status, dumps(meta or {})))
        self.con.execute("UPDATE conversations SET leaf_id=?, updated_ms=? WHERE id=?", (mid, t, cid))
        if status == "ok":
            self._index(cid, mid, blocks)
        return self.message(mid)

    def message(self, mid: str) -> dict | None:
        r = db.row(self.con.execute("SELECT * FROM messages WHERE id=?", (mid,)))
        return _msg(r) if r else None

    def update_message(self, mid: str, blocks=None, status=None, meta=None, model=None) -> None:
        sets, args = [], []
        if blocks is not None:
            sets.append("blocks=?")
            args.append(dumps(blocks))
        if status is not None:
            sets.append("status=?")
            args.append(status)
        if meta is not None:
            sets.append("meta=?")
            args.append(dumps(meta))
        if model is not None:
            sets.append("model=?")
            args.append(model)
        sets.append("updated_ms=?")
        args += [now_ms(), mid]
        self.con.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", args)
        if status in ("ok", "stopped", "error"):
            m = self.message(mid)
            if m:
                self._index(m["conv_id"], mid, m["blocks"])

    def _index(self, cid: str, mid: str, blocks: list) -> None:
        text = block_text(blocks)
        self.con.execute("DELETE FROM search WHERE msg_id=?", (mid,))
        if text.strip():
            self.con.execute("INSERT INTO search(text, conv_id, msg_id) VALUES (?,?,?)", (text, cid, mid))

    def reparent(self, mid: str, new_parent) -> None:
        self.con.execute("UPDATE messages SET parent_id=? WHERE id=?", (new_parent, mid))

    def copy_thread(self, cid: str, upto_id: str, title: str) -> dict:
        """A new conversation holding copies of the messages from the root to `upto_id` (fork)."""
        src = self.conversation(cid)
        new = self.create_conversation(title=title, model=src["model"], mode=src["mode"], folder=src["folder"],
                                       settings={k: v for k, v in src["settings"].items() if k != "todos"},
                                       project_id=src["project_id"])
        parent = None
        for m in self.path(upto_id):
            copy = self.add_message(new["id"], parent, m["role"], m["blocks"], m["model"], "ok", m["meta"])
            parent = copy["id"]
        return self.conversation(new["id"])

    def children(self, parent_id, cid: str) -> list[dict]:
        if parent_id is None:
            q = self.con.execute("SELECT * FROM messages WHERE conv_id=? AND parent_id IS NULL ORDER BY created_ms", (cid,))
        else:
            q = self.con.execute("SELECT * FROM messages WHERE parent_id=? ORDER BY created_ms", (parent_id,))
        return [_msg(r) for r in db.rows(q)]

    def path(self, leaf_id) -> list[dict]:
        """Messages from the root to `leaf_id`, oldest first."""
        out, mid, guard = [], leaf_id, 0
        while mid and guard < 100000:
            m = self.message(mid)
            if m is None:
                break
            out.append(m)
            mid = m["parent_id"]
            guard += 1
        out.reverse()
        return out

    def descend(self, mid: str) -> str:
        """The newest leaf below `mid`, following the most recent child at each step."""
        m = self.message(mid)
        while m:
            kids = self.children(m["id"], m["conv_id"])
            if not kids:
                return m["id"]
            m = kids[-1]
        return mid

    def thread(self, cid: str) -> list[dict]:
        """The branch on screen, each message annotated with its sibling position."""
        conv = self.conversation(cid)
        if not conv or not conv["leaf_id"]:
            return []
        msgs = self.path(conv["leaf_id"])
        for m in msgs:
            sibs = self.children(m["parent_id"], cid)
            ids = [s["id"] for s in sibs]
            m["siblings"] = ids
            m["sibling_index"] = ids.index(m["id"]) if m["id"] in ids else 0
        return msgs

    def all_messages(self, cid: str) -> list[dict]:
        return [_msg(r) for r in db.rows(self.con.execute(
            "SELECT * FROM messages WHERE conv_id=? ORDER BY created_ms", (cid,)))]

    # search -------------------------------------------------------------------------------------
    def search(self, query: str, limit: int = 30, any_term: bool = False, project_id=None, exclude=None) -> list[dict]:
        """Full-text search over messages. `any_term`: match any word (ranked) instead of all of them."""
        fts = _fts_any(query) if any_term else _fts_query(query)
        if not fts:
            return []
        rows_ = db.rows(self.con.execute(
            "SELECT conv_id, msg_id, snippet(search, 0, '[[', ']]', ' … ', 12) AS snippet, rank FROM search"
            " WHERE search MATCH ? ORDER BY rank LIMIT ?", (fts, limit * 4)))
        out = []
        for r in rows_:
            conv = self.conversation(r["conv_id"])
            if not conv or conv["incognito"] or r["conv_id"] == exclude:
                continue
            if project_id and conv["project_id"] != project_id:
                continue
            out.append({"conv_id": r["conv_id"], "msg_id": r["msg_id"], "snippet": r["snippet"], "title": conv["title"],
                        "updated_ms": conv["updated_ms"]})
            if len(out) >= limit:
                break
        return out

    # attachments --------------------------------------------------------------------------------
    def add_attachment(self, name, mime, size, kind, path, text=None, meta=None, conv_id=None) -> dict:
        aid = new_id()
        self.con.execute(
            "INSERT INTO attachments(id, conv_id, name, mime, size, kind, path, text, meta, created_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, conv_id, name, mime, size, kind, str(path), text, dumps(meta or {}), now_ms()))
        return self.attachment(aid)

    def attachment(self, aid: str) -> dict | None:
        r = db.row(self.con.execute("SELECT * FROM attachments WHERE id=?", (aid,)))
        if r:
            r["meta"] = loads(r["meta"], {})
        return r

    def generated_images(self, limit: int = 200, before_ms: int | None = None) -> list[dict]:
        """Images made by an image model, newest first (not from incognito conversations)."""
        q = ("SELECT a.id, a.conv_id, a.name, a.size, a.meta, a.created_ms FROM attachments a "
             "LEFT JOIN conversations c ON c.id=a.conv_id WHERE a.kind='image' AND a.meta LIKE '%\"generated\":true%' "
             "AND COALESCE(c.incognito, 0)=0")
        args: list = []
        if before_ms:
            q += " AND a.created_ms < ?"
            args.append(before_ms)
        out = []
        for r in db.rows(self.con.execute(q + " ORDER BY a.created_ms DESC LIMIT ?", args + [limit])):
            r["meta"] = loads(r["meta"], {})
            out.append(r)
        return out

    # artifacts ----------------------------------------------------------------------------------
    def create_artifact(self, cid, msg_id, title, kind, content, language=None) -> dict:
        aid, t = new_id(), now_ms()
        with db.Tx(self.con):
            self.con.execute(
                "INSERT INTO artifacts(id, conv_id, msg_id, title, kind, language, version, created_ms, updated_ms)"
                " VALUES (?,?,?,?,?,?,1,?,?)", (aid, cid, msg_id, title, kind, language, t, t))
            self.con.execute("INSERT INTO artifact_versions(artifact_id, version, content, msg_id, created_ms) VALUES (?,1,?,?,?)",
                             (aid, content, msg_id, t))
        return self.artifact(aid)

    def update_artifact(self, aid, content, msg_id=None, title=None) -> dict:
        with db.Tx(self.con):
            a = db.row(self.con.execute("SELECT * FROM artifacts WHERE id=?", (aid,)))
            if a is None:
                raise KeyError(aid)
            v = a["version"] + 1
            t = now_ms()
            self.con.execute("INSERT INTO artifact_versions(artifact_id, version, content, msg_id, created_ms) VALUES (?,?,?,?,?)",
                             (aid, v, content, msg_id, t))
            self.con.execute("UPDATE artifacts SET version=?, updated_ms=?, title=COALESCE(?, title) WHERE id=?",
                             (v, t, title, aid))
        return self.artifact(aid)

    def artifact(self, aid, version=None) -> dict | None:
        a = db.row(self.con.execute("SELECT * FROM artifacts WHERE id=?", (aid,)))
        if a is None:
            return None
        v = version or a["version"]
        r = self.con.execute("SELECT content, created_ms FROM artifact_versions WHERE artifact_id=? AND version=?", (aid, v)).fetchone()
        a["content"] = r[0] if r else ""
        a["shown_version"] = v
        return a

    def artifacts(self, cid=None, limit=200) -> list[dict]:
        if cid:
            q = self.con.execute("SELECT * FROM artifacts WHERE conv_id=? ORDER BY created_ms", (cid,))
        else:
            q = self.con.execute(
                "SELECT a.* FROM artifacts a JOIN conversations c ON c.id=a.conv_id WHERE c.incognito=0"
                " ORDER BY a.updated_ms DESC LIMIT ?", (limit,))
        return db.rows(q)

    # rules --------------------------------------------------------------------------------------
    def rules(self) -> list[dict]:
        return db.rows(self.con.execute("SELECT * FROM rules ORDER BY created_ms"))

    def add_rule(self, kind: str, pattern: str) -> dict:
        rid = new_id()
        self.con.execute("INSERT INTO rules(id, kind, pattern, created_ms) VALUES (?,?,?,?)", (rid, kind, pattern.strip(), now_ms()))
        return {"id": rid, "kind": kind, "pattern": pattern.strip()}

    def delete_rule(self, rid: str) -> None:
        self.con.execute("DELETE FROM rules WHERE id=?", (rid,))

    def auto_rules(self) -> list[dict]:
        return db.rows(self.con.execute("SELECT * FROM auto_rules ORDER BY created_ms"))

    def add_auto_rule(self, kind: str, text: str) -> dict:
        rid = new_id()
        self.con.execute("INSERT INTO auto_rules(id, kind, text, created_ms) VALUES (?,?,?,?)", (rid, kind, text.strip(), now_ms()))
        return {"id": rid, "kind": kind, "text": text.strip()}

    def delete_auto_rule(self, rid: str) -> None:
        self.con.execute("DELETE FROM auto_rules WHERE id=?", (rid,))

    # checkpoints --------------------------------------------------------------------------------
    def add_checkpoint(self, cid, msg_id, folder, manifest: dict) -> str:
        ckid = new_id()
        self.con.execute("INSERT INTO checkpoints(id, conv_id, msg_id, folder, manifest, created_ms) VALUES (?,?,?,?,?,?)",
                         (ckid, cid, msg_id, folder, dumps(manifest), now_ms()))
        return ckid

    def checkpoint_for(self, msg_id) -> dict | None:
        r = db.row(self.con.execute("SELECT * FROM checkpoints WHERE msg_id=? ORDER BY created_ms LIMIT 1", (msg_id,)))
        if r:
            r["manifest"] = loads(r["manifest"], {})
        return r

    def latest_checkpoint(self, cid, folder) -> dict | None:
        r = db.row(self.con.execute(
            "SELECT * FROM checkpoints WHERE conv_id=? AND folder=? ORDER BY created_ms DESC LIMIT 1", (cid, folder)))
        if r:
            r["manifest"] = loads(r["manifest"], {})
        return r

    # projects -----------------------------------------------------------------------------------
    def create_project(self, name: str, description: str = "", instructions: str = "", settings=None) -> dict:
        pid, t = new_id(), now_ms()
        self.con.execute(
            "INSERT INTO projects(id, name, description, instructions, settings, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?)",
            (pid, name, description, instructions, dumps(settings or {}), t, t))
        return self.project(pid)

    def project(self, pid: str) -> dict | None:
        r = db.row(self.con.execute("SELECT * FROM projects WHERE id=?", (pid,)))
        if r is None:
            return None
        r["settings"] = loads(r["settings"], {})
        r["archived"] = bool(r["archived"])
        return r

    def projects(self, archived: bool = False) -> list[dict]:
        out = []
        for r in db.rows(self.con.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM project_files f WHERE f.project_id=p.id) AS files,"
                " (SELECT COUNT(*) FROM conversations c WHERE c.project_id=p.id AND c.incognito=0) AS conversations,"
                " (SELECT MAX(c.updated_ms) FROM conversations c WHERE c.project_id=p.id) AS last_ms"
                " FROM projects p WHERE archived=? ORDER BY COALESCE(last_ms, p.updated_ms) DESC", (int(archived),))):
            r["settings"] = loads(r["settings"], {})
            r["archived"] = bool(r["archived"])
            out.append(r)
        return out

    def update_project(self, pid: str, **fields) -> dict:
        allowed = {"name", "description", "instructions", "settings", "archived"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"cannot update {k}")
            sets.append(f"{k}=?")
            args.append(dumps(v) if k == "settings" else (int(v) if isinstance(v, bool) else v))
        sets.append("updated_ms=?")
        args += [now_ms(), pid]
        self.con.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", args)
        return self.project(pid)

    def delete_project(self, pid: str) -> None:
        """The project, its files and its memory go; its conversations stay, outside any project."""
        with db.Tx(self.con):
            self.con.execute("DELETE FROM chunk_search WHERE project_id=?", (pid,))
            self.con.execute("DELETE FROM chunks WHERE project_id=?", (pid,))
            self.con.execute("DELETE FROM project_files WHERE project_id=?", (pid,))
            self.con.execute("DELETE FROM memories WHERE project_id=?", (pid,))
            self.con.execute("UPDATE conversations SET project_id=NULL WHERE project_id=?", (pid,))
            self.con.execute("DELETE FROM projects WHERE id=?", (pid,))

    def add_project_file(self, pid, name, mime, size, kind, path, text) -> dict:
        fid, t = new_id(), now_ms()
        self.con.execute(
            "INSERT INTO project_files(id, project_id, name, mime, size, kind, path, text, created_ms, updated_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", (fid, pid, name, mime, size, kind, str(path) if path else None, text or "", t, t))
        self.con.execute("UPDATE projects SET updated_ms=? WHERE id=?", (t, pid))
        return self.project_file(fid, with_text=False)

    def project_file(self, fid: str, with_text: bool = True) -> dict | None:
        cols = "*" if with_text else ("id, project_id, name, mime, size, kind, path, status, error, embed_model,"
                                      " created_ms, updated_ms, length(text) AS chars")
        r = db.row(self.con.execute(f"SELECT {cols} FROM project_files WHERE id=?", (fid,)))
        if r is not None and with_text:
            r["chars"] = len(r["text"])
        return r

    def project_files(self, pid: str) -> list[dict]:
        return db.rows(self.con.execute(
            "SELECT id, project_id, name, mime, size, kind, path, status, error, embed_model, created_ms, updated_ms,"
            " length(text) AS chars, (SELECT COUNT(*) FROM chunks c WHERE c.file_id=f.id) AS chunks"
            " FROM project_files f WHERE project_id=? ORDER BY created_ms", (pid,)))

    def set_file_status(self, fid: str, status: str, error=None, embed_model=None) -> None:
        self.con.execute("UPDATE project_files SET status=?, error=?, embed_model=?, updated_ms=? WHERE id=?",
                         (status, error, embed_model, now_ms(), fid))

    def delete_project_file(self, fid: str) -> None:
        with db.Tx(self.con):
            self.con.execute("DELETE FROM chunk_search WHERE chunk_id IN (SELECT id FROM chunks WHERE file_id=?)", (fid,))
            self.con.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
            self.con.execute("DELETE FROM project_files WHERE id=?", (fid,))

    def replace_chunks(self, fid: str, pid: str, chunks: list[tuple[str, bytes | None]]) -> None:
        with db.Tx(self.con):
            self.con.execute("DELETE FROM chunk_search WHERE chunk_id IN (SELECT id FROM chunks WHERE file_id=?)", (fid,))
            self.con.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
            for seq, (text, vector) in enumerate(chunks):
                cur = self.con.execute("INSERT INTO chunks(file_id, project_id, seq, text, vector) VALUES (?,?,?,?,?)",
                                       (fid, pid, seq, text, vector))
                self.con.execute("INSERT INTO chunk_search(text, chunk_id, project_id) VALUES (?,?,?)",
                                 (text, cur.lastrowid, pid))

    def chunk_vectors(self, pid: str, model: str) -> tuple[list[int], bytes, int]:
        """(chunk ids, their vectors concatenated as float32, dimension) for the files embedded with `model`."""
        ids, parts, dim = [], [], 0
        for cid, vec in self.con.execute(
                "SELECT c.id, c.vector FROM chunks c JOIN project_files f ON f.id=c.file_id"
                " WHERE c.project_id=? AND f.embed_model=? AND c.vector IS NOT NULL ORDER BY c.id", (pid, model)):
            d = len(vec) // 4
            if dim and d != dim:
                continue
            dim = d
            ids.append(cid)
            parts.append(vec)
        return ids, b"".join(parts), dim

    def chunk_keyword(self, pid: str, query: str, limit: int = 30) -> list[int]:
        fts = _fts_any(query)
        if not fts:
            return []
        return [r[0] for r in self.con.execute(
            "SELECT chunk_id FROM chunk_search WHERE chunk_search MATCH ? AND project_id=? ORDER BY rank LIMIT ?",
            (fts, pid, limit))]

    def chunks_by_ids(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        rows_ = db.rows(self.con.execute(
            f"SELECT c.id, c.file_id, c.seq, c.text, f.name FROM chunks c JOIN project_files f ON f.id=c.file_id"
            f" WHERE c.id IN ({','.join('?' for _ in ids)})", ids))
        order = {cid: i for i, cid in enumerate(ids)}
        return sorted(rows_, key=lambda r: order[r["id"]])

    # memory -------------------------------------------------------------------------------------
    def memories(self, project_id=None, scope: str = "both") -> list[dict]:
        """scope 'both': account-wide plus the project's; 'account'; 'project'; 'all' (every project too)."""
        if scope == "all":
            q = self.con.execute("SELECT * FROM memories ORDER BY created_ms")
        elif scope == "project":
            q = self.con.execute("SELECT * FROM memories WHERE project_id=? ORDER BY created_ms", (project_id,))
        elif scope == "account" or not project_id:
            q = self.con.execute("SELECT * FROM memories WHERE project_id IS NULL ORDER BY created_ms")
        else:
            q = self.con.execute("SELECT * FROM memories WHERE project_id IS NULL OR project_id=? ORDER BY created_ms",
                                 (project_id,))
        return db.rows(q)

    def memory(self, mid: str) -> dict | None:
        return db.row(self.con.execute("SELECT * FROM memories WHERE id=?", (mid,)))

    def add_memory(self, text: str, project_id=None, source: str = "user", conv_id=None) -> dict:
        mid, t = new_id(), now_ms()
        self.con.execute("INSERT INTO memories(id, project_id, text, source, conv_id, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?)",
                         (mid, project_id, text.strip(), source, conv_id, t, t))
        return self.memory(mid)

    def update_memory(self, mid: str, text: str) -> dict:
        self.con.execute("UPDATE memories SET text=?, updated_ms=? WHERE id=?", (text.strip(), now_ms(), mid))
        return self.memory(mid)

    def delete_memory(self, mid: str) -> None:
        self.con.execute("DELETE FROM memories WHERE id=?", (mid,))

    # schedules ----------------------------------------------------------------------------------
    def schedules(self) -> list[dict]:
        return [_schedule(r) for r in db.rows(self.con.execute("SELECT * FROM schedules ORDER BY created_ms"))]

    def schedule(self, sid: str) -> dict | None:
        r = db.row(self.con.execute("SELECT * FROM schedules WHERE id=?", (sid,)))
        return _schedule(r) if r else None

    def add_schedule(self, title: str, prompt: str, spec: dict, settings: dict, next_ms: int | None) -> dict:
        sid, t = new_id(), now_ms()
        self.con.execute("INSERT INTO schedules(id, title, prompt, spec, settings, next_ms, created_ms, updated_ms)"
                         " VALUES (?,?,?,?,?,?,?,?)", (sid, title, prompt, dumps(spec), dumps(settings), next_ms, t, t))
        return self.schedule(sid)

    def update_schedule(self, sid: str, **fields) -> dict:
        allowed = {"title", "prompt", "spec", "settings", "enabled", "next_ms", "last_ms", "last_conv", "last_status", "runs"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"cannot update {k}")
            sets.append(f"{k}=?")
            args.append(dumps(v) if k in ("spec", "settings") else (int(v) if isinstance(v, bool) else v))
        sets.append("updated_ms=?")
        args += [now_ms(), sid]
        self.con.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE id=?", args)
        return self.schedule(sid)

    def delete_schedule(self, sid: str) -> None:
        self.con.execute("DELETE FROM schedules WHERE id=?", (sid,))

    def due_schedules(self, now: int) -> list[dict]:
        return [_schedule(r) for r in db.rows(self.con.execute(
            "SELECT * FROM schedules WHERE enabled=1 AND next_ms IS NOT NULL AND next_ms <= ? ORDER BY next_ms", (now,)))]


def _schedule(r: dict) -> dict:
    r["spec"] = loads(r["spec"], {})
    r["settings"] = loads(r["settings"], {})
    r["enabled"] = bool(r["enabled"])
    return r


def _conv(r: dict) -> dict:
    r["settings"] = loads(r["settings"], {})
    for k in ("pinned", "archived", "incognito"):
        r[k] = bool(r[k])
    return r


def _msg(r: dict) -> dict:
    r["blocks"] = loads(r["blocks"], [])
    r["meta"] = loads(r["meta"], {})
    return r


def _fts_query(q: str) -> str:
    terms = [t.replace('"', '""') for t in q.split() if t.strip()]
    return " ".join(f'"{t}"*' for t in terms) or '""'


_STOP = set("""the and for are but not you all any can had her was one our out has him his how its may new now old see two
way who did get let put say she too use that with have this will your from they know want been good much some time very
when come here just like long make many more only over such take than them well were what where which while would there
their about after again also could does into most other should these those through under until upon being each few
both doing having itself myself please tell give show find""".split())


def _fts_any(q: str) -> str:
    """An FTS5 query matching any meaningful word of `q` (as a prefix), for ranked retrieval."""
    import re as _re
    words = [_stem(w) for w in _re.findall(r"\w+", q.lower()) if len(w) > 2 and w not in _STOP][:24]
    return " OR ".join(f'"{w}"*' for w in dict.fromkeys(words))


def _stem(w: str) -> str:
    """A light suffix cut so that a prefix query finds other forms: grazing -> graz* (graze, grazed)."""
    for suffix in ("ings", "ing", "ies", "ed", "es", "ly", "s"):
        if len(w) >= len(suffix) + 4 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


class Stores:
    """Opens each account's store on first use and keeps it."""

    def __init__(self, paths):
        self.paths = paths
        self._stores: dict[str, AccountStore] = {}

    def get(self, account_id: str) -> AccountStore:
        s = self._stores.get(account_id)
        if s is None:
            s = AccountStore(self.paths.account_db(account_id), account_id)
            self._stores[account_id] = s
        return s
