"""SQLite helpers: connections tuned for a single-process server, and numbered migrations."""

import sqlite3
from pathlib import Path


def connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def migrate(con: sqlite3.Connection, migrations: list[str]) -> None:
    """Apply migrations[user_version:] in order. Each entry is a SQL script."""
    version = con.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(migrations[version:], start=version + 1):
        con.execute("BEGIN")
        try:
            for statement in _split(script):
                con.execute(statement)
            con.execute(f"PRAGMA user_version={number}")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise


def _split(script: str) -> list[str]:
    """Split a script on ';' at statement ends (the scripts here contain no triggers with bodies)."""
    parts, buf = [], []
    for line in script.splitlines():
        buf.append(line)
        if line.rstrip().endswith(";") and sqlite3.complete_statement("\n".join(buf)):
            parts.append("\n".join(buf))
            buf = []
    if "".join(buf).strip():
        parts.append("\n".join(buf))
    return [p for p in parts if p.strip()]


class Tx:
    """`with Tx(con):` runs a block in one transaction."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def __enter__(self):
        self.con.execute("BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, exc_type, exc, tb):
        self.con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


def row(cursor) -> dict | None:
    r = cursor.fetchone()
    return dict(r) if r else None
