"""The machine-level database: accounts, sessions, settings, models, jobs, folder grants."""

from . import db
from .util import dumps, loads, now_ms

MIGRATIONS = [
    """
    CREATE TABLE accounts (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        color TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('owner', 'user')),
        pw_hash TEXT,
        require_owner_approval INTEGER NOT NULL DEFAULT 0,
        settings TEXT NOT NULL DEFAULT '{}',
        disabled INTEGER NOT NULL DEFAULT 0,
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        csrf TEXT NOT NULL,
        client TEXT NOT NULL,
        ip TEXT,
        created_ms INTEGER NOT NULL,
        last_seen_ms INTEGER NOT NULL,
        expires_ms INTEGER NOT NULL
    );
    CREATE INDEX sessions_account ON sessions(account_id);
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE models (
        name TEXT PRIMARY KEY,
        digest TEXT,
        role TEXT NOT NULL DEFAULT 'chat',
        approved INTEGER NOT NULL DEFAULT 0,
        approved_ms INTEGER,
        num_ctx INTEGER,
        fit TEXT NOT NULL DEFAULT '{}',
        info TEXT NOT NULL DEFAULT '{}',
        note TEXT,
        updated_ms INTEGER NOT NULL
    );
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        account_id TEXT,
        params TEXT NOT NULL DEFAULT '{}',
        progress TEXT NOT NULL DEFAULT '{}',
        result TEXT NOT NULL DEFAULT '{}',
        error TEXT,
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL
    );
    CREATE INDEX jobs_created ON jobs(created_ms);
    CREATE TABLE grants (
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        access TEXT NOT NULL CHECK (access IN ('ro', 'rw')),
        created_ms INTEGER NOT NULL,
        PRIMARY KEY (account_id, path)
    );
    CREATE TABLE trusted_folders (
        path TEXT PRIMARY KEY,
        trusted_by TEXT,
        created_ms INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE api_keys (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        created_ms INTEGER NOT NULL,
        last_used_ms INTEGER
    );
    """,
    """
    ALTER TABLE models ADD COLUMN runtime TEXT NOT NULL DEFAULT 'ollama';
    ALTER TABLE models ADD COLUMN source TEXT NOT NULL DEFAULT '{}';
    """,
    """
    CREATE TABLE shares (
        id TEXT PRIMARY KEY,
        from_account TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        recipients TEXT NOT NULL,
        conv_id TEXT NOT NULL,
        title TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        created_ms INTEGER NOT NULL
    );
    CREATE INDEX shares_from ON shares(from_account, created_ms);
    """,
    # Who may connect, where nobody has chosen yet: a data folder that already has accounts dates from before the
    # choice existed and keeps the local network; a new one starts with this computer only.
    """
    INSERT OR IGNORE INTO settings (key, value)
        SELECT 'network', CASE WHEN EXISTS (SELECT 1 FROM accounts) THEN '"lan"' ELSE '"local"' END;
    """,
]


class MainDB:
    def __init__(self, path):
        self.con = db.connect(path)
        db.migrate(self.con, MIGRATIONS)

    # settings -----------------------------------------------------------------------------------
    def get_setting(self, key: str, default=None):
        r = self.con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return loads(r[0], default) if r else default

    def set_setting(self, key: str, value) -> None:
        self.con.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, dumps(value)),
        )

    # jobs ---------------------------------------------------------------------------------------
    def job_create(self, job_id: str, kind: str, account_id: str | None, params: dict) -> None:
        t = now_ms()
        self.con.execute(
            "INSERT INTO jobs(id, kind, status, account_id, params, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?)",
            (job_id, kind, "running", account_id, dumps(params), t, t),
        )

    def job_update(self, job_id: str, **fields) -> None:
        sets, args = [], []
        for key, value in fields.items():
            sets.append(f"{key}=?")
            args.append(dumps(value) if key in ("params", "progress", "result") else value)
        sets.append("updated_ms=?")
        args.append(now_ms())
        args.append(job_id)
        self.con.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", args)

    def job_get(self, job_id: str) -> dict | None:
        return _job(db.row(self.con.execute("SELECT * FROM jobs WHERE id=?", (job_id,))))

    def jobs_recent(self, limit: int = 50) -> list[dict]:
        return [_job(r) for r in db.rows(self.con.execute("SELECT * FROM jobs ORDER BY created_ms DESC LIMIT ?", (limit,)))]

    # shares -------------------------------------------------------------------------------------
    def share_create(self, share_id: str, from_account: str, recipients: list, conv_id: str, title: str, snapshot: dict) -> None:
        self.con.execute("INSERT INTO shares(id, from_account, recipients, conv_id, title, snapshot, created_ms) VALUES (?,?,?,?,?,?,?)",
                         (share_id, from_account, dumps(recipients), conv_id, title, dumps(snapshot), now_ms()))

    def share_get(self, share_id: str) -> dict | None:
        r = db.row(self.con.execute("SELECT * FROM shares WHERE id=?", (share_id,)))
        if r:
            r["recipients"] = loads(r["recipients"], [])
            r["snapshot"] = loads(r["snapshot"], {})
        return r

    def shares_for(self, account_id: str) -> tuple[list[dict], list[dict]]:
        """(shared with this account, shared by it), newest first, without the snapshots."""
        rows = db.rows(self.con.execute("SELECT id, from_account, recipients, conv_id, title, created_ms FROM shares "
                                        "ORDER BY created_ms DESC LIMIT 500"))
        mine, theirs = [], []
        for r in rows:
            r["recipients"] = loads(r["recipients"], [])
            if r["from_account"] == account_id:
                mine.append(r)
            elif "*" in r["recipients"] or account_id in r["recipients"]:
                theirs.append(r)
        return theirs, mine

    def share_delete(self, share_id: str) -> None:
        self.con.execute("DELETE FROM shares WHERE id=?", (share_id,))

    # trusted folders ----------------------------------------------------------------------------
    def is_trusted(self, path: str) -> bool:
        return self.con.execute("SELECT 1 FROM trusted_folders WHERE path=?", (path,)).fetchone() is not None

    def trust(self, path: str, account_id: str) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO trusted_folders(path, trusted_by, created_ms) VALUES (?,?,?)", (path, account_id, now_ms())
        )


def _job(r: dict | None) -> dict | None:
    if r is None:
        return None
    for key in ("params", "progress", "result"):
        r[key] = loads(r[key], {})
    return r
