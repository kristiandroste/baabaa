"""Usage statistics: event-level metadata, never content.

One SQLite database for the whole machine. Every row carries the account id, so an account sees its
own rows and the owner sees all. Timestamps are UTC milliseconds; durations are milliseconds from a
monotonic clock. The schema is documented in docs/STATISTICS.md and versioned with PRAGMA user_version.
"""

import csv
import io
import json
import sqlite3
import statistics

from . import db
from .util import dumps, new_id, now_ms

MIGRATIONS = [
    """
    CREATE TABLE model_requests (
        id TEXT PRIMARY KEY,
        ts_ms INTEGER NOT NULL,
        account_id TEXT,
        conv_id TEXT,
        msg_id TEXT,
        window TEXT,
        kind TEXT NOT NULL,
        model TEXT NOT NULL,
        num_ctx INTEGER,
        think TEXT,
        queue_wait_ms INTEGER,
        load_ms INTEGER,
        ttft_ms INTEGER,
        prompt_tokens INTEGER,
        output_tokens INTEGER,
        prompt_eval_ms INTEGER,
        eval_ms INTEGER,
        total_ms INTEGER,
        wall_ms INTEGER,
        tokens_per_s REAL,
        outcome TEXT NOT NULL,
        error TEXT,
        residency_ok INTEGER,
        model_bytes INTEGER,
        vram_bytes INTEGER,
        energy_mj INTEGER,
        gaps TEXT
    );
    CREATE INDEX mr_ts ON model_requests(ts_ms);
    CREATE INDEX mr_account ON model_requests(account_id, ts_ms);
    CREATE TABLE gpu_samples (
        ts_ms INTEGER NOT NULL,
        vram_used INTEGER,
        util_gpu INTEGER,
        util_mem INTEGER,
        power_mw INTEGER,
        temp_c INTEGER
    );
    CREATE INDEX gs_ts ON gpu_samples(ts_ms);
    CREATE TABLE tool_calls (
        id TEXT PRIMARY KEY,
        ts_ms INTEGER NOT NULL,
        account_id TEXT,
        conv_id TEXT,
        msg_id TEXT,
        tool TEXT NOT NULL,
        mode TEXT,
        decision TEXT,
        layer TEXT,
        duration_ms INTEGER,
        exit_code INTEGER,
        output_bytes INTEGER,
        sandbox_denied INTEGER,
        error TEXT
    );
    CREATE INDEX tc_ts ON tool_calls(ts_ms);
    CREATE TABLE approvals (
        id TEXT PRIMARY KEY,
        ts_ms INTEGER NOT NULL,
        account_id TEXT,
        conv_id TEXT,
        tool_call_id TEXT,
        tool TEXT,
        mode TEXT,
        layer TEXT,
        judge_risk TEXT,
        judge_user_asked TEXT,
        judge_decision TEXT,
        final TEXT,
        approver_id TEXT,
        latency_ms INTEGER
    );
    CREATE INDEX ap_ts ON approvals(ts_ms);
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_ms INTEGER NOT NULL,
        account_id TEXT,
        conv_id TEXT,
        window TEXT,
        type TEXT NOT NULL,
        data TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX ev_ts ON events(ts_ms);
    CREATE INDEX ev_type ON events(type, ts_ms);
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        ts_ms INTEGER NOT NULL,
        account_id TEXT,
        kind TEXT NOT NULL,
        model TEXT,
        duration_ms INTEGER,
        bytes INTEGER,
        outcome TEXT,
        data TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX jb_ts ON jobs(ts_ms);
    """,
    # 2: CPU use of the program serving each request (the CPU guard, cpuwatch.py)
    """
    ALTER TABLE model_requests ADD COLUMN cpu_s REAL;
    ALTER TABLE model_requests ADD COLUMN cpu_peak REAL;
    """,
]

TABLES = ("model_requests", "gpu_samples", "tool_calls", "approvals", "events", "jobs")


class Stats:
    def __init__(self, path):
        self.path = str(path)
        self.con = db.connect(path)
        db.migrate(self.con, MIGRATIONS)
        self.enabled = True

    def _insert(self, table: str, values: dict) -> None:
        if not self.enabled:
            return
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        try:
            self.con.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))
        except sqlite3.Error:
            # Statistics must never break a conversation.
            pass

    # recording ----------------------------------------------------------------------------------
    def model_request(self, **v) -> str:
        v.setdefault("id", new_id())
        v.setdefault("ts_ms", now_ms())
        if isinstance(v.get("gaps"), list):
            v["gaps"] = dumps(v["gaps"])
        self._insert("model_requests", v)
        return v["id"]

    def gpu_sample(self, sample: dict) -> None:
        self._insert("gpu_samples", {
            "ts_ms": now_ms(),
            "vram_used": sample.get("vram_used"),
            "util_gpu": sample.get("util_gpu"),
            "util_mem": sample.get("util_mem"),
            "power_mw": sample.get("power_mw"),
            "temp_c": sample.get("temp_c"),
        })

    def tool_call(self, **v) -> None:
        v.setdefault("id", new_id())
        v.setdefault("ts_ms", now_ms())
        self._insert("tool_calls", v)

    def approval(self, **v) -> None:
        v.setdefault("id", new_id())
        v.setdefault("ts_ms", now_ms())
        self._insert("approvals", v)

    def event(self, type: str, account_id=None, conv_id=None, window=None, **data) -> None:
        self._insert("events", {
            "ts_ms": now_ms(), "account_id": account_id, "conv_id": conv_id, "window": window,
            "type": type, "data": dumps(data),
        })

    def job(self, **v) -> None:
        v.setdefault("id", new_id())
        v.setdefault("ts_ms", now_ms())
        if isinstance(v.get("data"), dict):
            v["data"] = dumps(v["data"])
        self._insert("jobs", v)

    # reading ------------------------------------------------------------------------------------
    def summary(self, account_id: str | None, from_ms: int, to_ms: int, group: str = "day", tz_offset_min: int = 0) -> dict:
        """Totals plus rows grouped by day, model, account, kind or window. account_id None = all."""
        where, args = ["ts_ms >= ?", "ts_ms < ?"], [from_ms, to_ms]
        if account_id:
            where.append("account_id = ?")
            args.append(account_id)
        clause = " AND ".join(where)
        if group == "day":
            key = f"date((ts_ms / 1000) + {int(tz_offset_min) * 60}, 'unixepoch')"
        elif group in ("model", "account_id", "kind", "window", "outcome"):
            key = group
        else:
            raise ValueError("unknown grouping")
        q = f"""
            SELECT {key} AS k, COUNT(*) AS requests,
                   SUM(COALESCE(prompt_tokens,0)) AS prompt_tokens,
                   SUM(COALESCE(output_tokens,0)) AS output_tokens,
                   SUM(COALESCE(total_ms,0)) AS gpu_ms,
                   SUM(COALESCE(energy_mj,0)) AS energy_mj,
                   SUM(CASE WHEN outcome='stopped' THEN 1 ELSE 0 END) AS stopped,
                   SUM(CASE WHEN outcome='error' THEN 1 ELSE 0 END) AS errors
            FROM model_requests WHERE {clause} GROUP BY k ORDER BY k
        """
        groups = db.rows(self.con.execute(q, args))
        total = db.row(self.con.execute(f"""
            SELECT COUNT(*) AS requests,
                   SUM(COALESCE(prompt_tokens,0)) AS prompt_tokens,
                   SUM(COALESCE(output_tokens,0)) AS output_tokens,
                   SUM(COALESCE(total_ms,0)) AS gpu_ms,
                   SUM(COALESCE(energy_mj,0)) AS energy_mj,
                   COUNT(DISTINCT conv_id) AS conversations
            FROM model_requests WHERE {clause}""", args)) or {}
        speeds = [r[0] for r in self.con.execute(
            f"SELECT tokens_per_s FROM model_requests WHERE {clause} AND kind='chat' AND tokens_per_s IS NOT NULL", args)]
        ttfts = [r[0] for r in self.con.execute(
            f"SELECT ttft_ms FROM model_requests WHERE {clause} AND kind='chat' AND ttft_ms IS NOT NULL", args)]
        waits = [r[0] for r in self.con.execute(
            f"SELECT queue_wait_ms FROM model_requests WHERE {clause} AND queue_wait_ms IS NOT NULL", args)]
        total["tokens_per_s_median"] = round(statistics.median(speeds), 1) if speeds else None
        total["ttft_ms_median"] = round(statistics.median(ttfts)) if ttfts else None
        total["queue_wait_ms_median"] = round(statistics.median(waits)) if waits else None
        tools = db.rows(self.con.execute(f"""
            SELECT tool, COUNT(*) AS calls, SUM(COALESCE(duration_ms,0)) AS ms,
                   SUM(CASE WHEN exit_code IS NOT NULL AND exit_code != 0 THEN 1 ELSE 0 END) AS failed
            FROM tool_calls WHERE {clause} GROUP BY tool ORDER BY calls DESC""", args))
        decisions = db.rows(self.con.execute(f"""
            SELECT layer, judge_decision, final, COUNT(*) AS n FROM approvals
            WHERE {clause} GROUP BY layer, judge_decision, final ORDER BY n DESC""", args))
        return {"total": total, "groups": groups, "tools": tools, "approvals": decisions, "group": group}

    def gpu_series(self, from_ms: int, to_ms: int, max_points: int = 600) -> list[dict]:
        rows_ = db.rows(self.con.execute(
            "SELECT * FROM gpu_samples WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms", (from_ms, to_ms)))
        if len(rows_) <= max_points:
            return rows_
        step = len(rows_) / max_points
        return [rows_[int(i * step)] for i in range(max_points)]

    def export(self, table: str, fmt: str, account_id: str | None, from_ms: int, to_ms: int):
        """Yield chunks of CSV or JSON Lines for one table."""
        if table not in TABLES:
            raise ValueError("unknown table")
        where, args = ["ts_ms >= ?", "ts_ms < ?"], [from_ms, to_ms]
        if account_id and table != "gpu_samples":
            where.append("account_id = ?")
            args.append(account_id)
        cur = self.con.execute(f"SELECT * FROM {table} WHERE {' AND '.join(where)} ORDER BY ts_ms", args)
        cols = [d[0] for d in cur.description]
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            for r in cur:
                w.writerow(list(r))
                if buf.tell() > 65536:
                    yield buf.getvalue().encode()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue().encode()
        elif fmt == "jsonl":
            batch = []
            for r in cur:
                batch.append(json.dumps(dict(zip(cols, r)), ensure_ascii=False))
                if len(batch) >= 500:
                    yield ("\n".join(batch) + "\n").encode()
                    batch = []
            if batch:
                yield ("\n".join(batch) + "\n").encode()
        else:
            raise ValueError("format is csv or jsonl")

    def snapshot(self, dest: str, account_id: str | None = None) -> None:
        """Copy the database to `dest`. For a non-owner, only that account's rows are kept."""
        out = sqlite3.connect(dest)
        with out:
            self.con.backup(out)
        if account_id:
            for table in TABLES:
                if table != "gpu_samples":
                    out.execute(f"DELETE FROM {table} WHERE account_id IS NOT ? ", (account_id,))
            out.commit()
            out.execute("VACUUM")
        out.close()
