"""Accounts, passwords (scrypt, optional) and login sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time

from . import db
from .util import dumps, loads, new_id, now_ms

SESSION_TTL_MS = 30 * 24 * 3600 * 1000
COLORS = ["#2f7d6d", "#7a5c99", "#b5563c", "#3d6fb6", "#8a7a2c", "#a0476f", "#4f7f3a", "#5b6470"]
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")

# scrypt cost: 32 MiB of memory and about 0.2 s per hash on a laptop CPU (measured 2026-10-05). The cost is
# stored with each hash, so older, cheaper hashes still check, and are renewed at the next sign-in.
_N, _R, _P = 2**15, 8, 3


class AccountError(Exception):
    pass


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=256 * _N * _R, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, digest = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        n, r, p = int(n), int(r), int(p)
        if not (1 < n <= 2**20 and 0 < r <= 32 and 0 < p <= 16):  # a stored cost nobody here ever wrote
            return False
        test = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * n * r, dklen=len(digest))
        return hmac.compare_digest(test, digest)
    except (ValueError, TypeError):
        return False


def outdated(stored: str) -> bool:
    """A hash made with a cost other than today's."""
    return stored.split("$")[1:4] != [str(_N), str(_R), str(_P)]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, maindb):
        self.db = maindb
        self.con = maindb.con
        self._failures: dict[str, list[float]] = {}

    # accounts -----------------------------------------------------------------------------------
    def count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

    def create(self, name: str, display_name: str = "", password: str | None = None, role: str = "user") -> dict:
        name = name.strip().lower()
        if not _NAME_RE.match(name):
            raise AccountError("Names use lowercase letters, digits, '.', '_' or '-', up to 32 characters.")
        if role not in ("owner", "user"):
            raise AccountError("Unknown role.")
        if self.con.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
            raise AccountError(f"An account named {name!r} already exists.")
        t = now_ms()
        account_id = new_id()
        color = COLORS[self.count() % len(COLORS)]
        self.con.execute(
            "INSERT INTO accounts(id, name, display_name, color, role, pw_hash, created_ms, updated_ms)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (account_id, name, display_name.strip() or name, color, role,
             hash_password(password) if password else None, t, t),
        )
        return self.get(account_id)

    def get(self, account_id: str) -> dict | None:
        r = db.row(self.con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)))
        return _public(r) if r else None

    def by_name(self, name: str) -> dict | None:
        r = db.row(self.con.execute("SELECT * FROM accounts WHERE name=?", (name.strip().lower(),)))
        return _public(r) if r else None

    def list(self) -> list[dict]:
        return [_public(r) for r in db.rows(self.con.execute("SELECT * FROM accounts ORDER BY created_ms"))]

    def owners(self) -> list[dict]:
        return [a for a in self.list() if a["role"] == "owner" and not a["disabled"]]

    def update(self, account_id: str, **fields) -> dict:
        allowed = {"display_name", "color", "role", "require_owner_approval", "disabled", "settings"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise AccountError(f"Cannot change {key}.")
            if key == "role" and value not in ("owner", "user"):
                raise AccountError("Unknown role.")
            sets.append(f"{key}=?")
            args.append(dumps(value) if key == "settings" else value)
        if not sets:
            return self.get(account_id)
        if fields.get("role") == "user" or fields.get("disabled"):
            others = [a for a in self.owners() if a["id"] != account_id]
            if not others and (self.get(account_id) or {}).get("role") == "owner":
                raise AccountError("There must always be at least one owner.")
        sets.append("updated_ms=?")
        args += [now_ms(), account_id]
        self.con.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", args)
        if fields.get("disabled"):
            self.con.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))
        return self.get(account_id)

    def update_settings(self, account_id: str, patch: dict) -> dict:
        current = self.get(account_id)["settings"]
        current.update(patch)
        return self.update(account_id, settings={k: v for k, v in current.items() if v is not None})

    def set_password(self, account_id: str, password: str | None) -> None:
        self.con.execute(
            "UPDATE accounts SET pw_hash=?, updated_ms=? WHERE id=?",
            (hash_password(password) if password else None, now_ms(), account_id),
        )
        self.con.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))

    def delete(self, account_id: str) -> None:
        account = self.get(account_id)
        if account and account["role"] == "owner" and len(self.owners()) <= 1:
            raise AccountError("The last owner cannot be deleted.")
        self.con.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    # login and sessions -------------------------------------------------------------------------
    def check_password(self, account_id: str, password: str | None, ip: str) -> bool:
        key = f"{account_id}|{ip}"
        recent = [t for t in self._failures.get(key, []) if time.monotonic() - t < 900]
        if len(recent) >= 10:
            raise AccountError("Too many attempts. Try again in a few minutes.")
        r = self.con.execute("SELECT pw_hash, disabled FROM accounts WHERE id=?", (account_id,)).fetchone()
        if r is None or r["disabled"]:
            return False
        ok = True if r["pw_hash"] is None else verify_password(password or "", r["pw_hash"])
        if not ok:
            recent.append(time.monotonic())
            self._failures[key] = recent
        elif r["pw_hash"] is not None and outdated(r["pw_hash"]):
            self.con.execute("UPDATE accounts SET pw_hash=? WHERE id=?", (hash_password(password), account_id))
        return ok

    def open_session(self, account_id: str, client: str, ip: str) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        t = now_ms()
        self.con.execute(
            "INSERT INTO sessions(token_hash, account_id, csrf, client, ip, created_ms, last_seen_ms, expires_ms)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (_token_hash(token), account_id, csrf, client, ip, t, t, t + SESSION_TTL_MS),
        )
        return token, csrf

    def session(self, token: str) -> dict | None:
        if not token:
            return None
        h = _token_hash(token)
        r = db.row(self.con.execute("SELECT * FROM sessions WHERE token_hash=?", (h,)))
        if r is None:
            return None
        t = now_ms()
        if r["expires_ms"] < t:
            self.con.execute("DELETE FROM sessions WHERE token_hash=?", (h,))
            return None
        if t - r["last_seen_ms"] > 60_000:
            self.con.execute(
                "UPDATE sessions SET last_seen_ms=?, expires_ms=? WHERE token_hash=?", (t, t + SESSION_TTL_MS, h)
            )
        account = self.get(r["account_id"])
        if account is None or account["disabled"]:
            return None
        r["account"] = account
        return r

    def close_session(self, token: str) -> None:
        self.con.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))

    # API keys (for scripts: Authorization: Bearer bk_…) --------------------------------------------
    def create_api_key(self, account_id: str, name: str) -> tuple[dict, str]:
        token = "bk_" + secrets.token_urlsafe(32)
        kid = new_id()
        self.con.execute("INSERT INTO api_keys(id, account_id, name, token_hash, created_ms) VALUES (?,?,?,?,?)",
                         (kid, account_id, (name or "key").strip()[:60], _token_hash(token), now_ms()))
        return {"id": kid, "name": name, "created_ms": now_ms(), "last_used_ms": None}, token

    def api_keys(self, account_id: str) -> list[dict]:
        return db.rows(self.con.execute(
            "SELECT id, name, created_ms, last_used_ms FROM api_keys WHERE account_id=? ORDER BY created_ms", (account_id,)))

    def revoke_api_key(self, account_id: str, key_id: str) -> None:
        self.con.execute("DELETE FROM api_keys WHERE id=? AND account_id=?", (key_id, account_id))

    def api_key_account(self, token: str) -> dict | None:
        r = db.row(self.con.execute("SELECT id, account_id, last_used_ms FROM api_keys WHERE token_hash=?", (_token_hash(token),)))
        if r is None:
            return None
        t = now_ms()
        if not r["last_used_ms"] or t - r["last_used_ms"] > 60_000:
            self.con.execute("UPDATE api_keys SET last_used_ms=? WHERE id=?", (t, r["id"]))
        account = self.get(r["account_id"])
        return None if account is None or account["disabled"] else {**account, "_key_id": r["id"]}

    # folder grants ------------------------------------------------------------------------------
    def grants(self, account_id: str) -> list[dict]:
        return db.rows(self.con.execute("SELECT path, access FROM grants WHERE account_id=? ORDER BY path", (account_id,)))

    def grant(self, account_id: str, path: str, access: str) -> None:
        if access not in ("ro", "rw"):
            raise AccountError("Access is 'ro' or 'rw'.")
        path = os.path.realpath(os.path.expanduser(path))
        if not os.path.isdir(path):
            raise AccountError(f"Not a folder: {path}")
        self.con.execute(
            "INSERT INTO grants(account_id, path, access, created_ms) VALUES (?,?,?,?)"
            " ON CONFLICT(account_id, path) DO UPDATE SET access=excluded.access",
            (account_id, path, access, now_ms()),
        )

    def revoke(self, account_id: str, path: str) -> None:
        self.con.execute("DELETE FROM grants WHERE account_id=? AND path=?", (account_id, path))

    def folder_access(self, account: dict, folder: str) -> str | None:
        """'rw', 'ro' or None: what this account may do in `folder`. Owners may use any folder; every account
        may use the git worktrees baabaa made for it (under its own data folder)."""
        folder = os.path.realpath(folder)
        if account["role"] == "owner":
            return "rw"
        own = getattr(self, "worktree_root", None)
        if own:
            root = os.path.realpath(own(account["id"]))
            if folder == root or folder.startswith(root + "/"):
                return "rw"
        best = None
        for g in self.grants(account["id"]):
            root = g["path"]
            if folder == root or folder.startswith(root.rstrip("/") + "/"):
                if best is None or g["access"] == "rw":
                    best = g["access"]
        return best


def _public(r: dict) -> dict:
    r = dict(r)
    r["has_password"] = r.pop("pw_hash", None) is not None
    r["settings"] = loads(r.get("settings"), {})
    r["require_owner_approval"] = bool(r.get("require_owner_approval"))
    r["disabled"] = bool(r.get("disabled"))
    return r
