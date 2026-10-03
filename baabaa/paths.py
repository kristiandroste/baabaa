"""Where baabaa keeps its data.

Everything lives under one data directory: `$BAABAA_HOME` if set, else `$XDG_DATA_HOME/baabaa`, else
`~/.local/share/baabaa`. Put it on fast local storage; it holds SQLite databases.
"""

import hashlib
import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("BAABAA_HOME")
    if env:
        base = Path(env).expanduser()
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        base = Path(xdg) / "baabaa"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


class Paths:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else data_dir()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def main_db(self) -> Path:
        return self.root / "baabaa.db"

    @property
    def stats_db(self) -> Path:
        return self.root / "stats.db"

    @property
    def tls(self) -> Path:
        return self._dir("tls")

    @property
    def socket(self) -> Path:
        """The terminal client's Unix socket. Socket paths are limited to 107 bytes, so a deep data
        folder gets its socket in the user's runtime directory instead."""
        path = self.root / "baabaa.sock"
        if len(str(path).encode()) < 100:
            return path
        base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/baabaa-{os.getuid()}"
        d = Path(base) / "baabaa"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d / (hashlib.sha256(str(self.root).encode()).hexdigest()[:16] + ".sock")

    def account(self, account_id: str) -> Path:
        return self._dir("accounts", account_id)

    def account_db(self, account_id: str) -> Path:
        return self.account(account_id) / "account.db"

    def account_files(self, account_id: str, *parts: str) -> Path:
        return self._dir("accounts", account_id, "files", *parts)

    def account_home(self, account_id: str) -> Path:
        """HOME for sandboxed tool processes of this account."""
        return self._dir("accounts", account_id, "home")

    def account_tmp(self, account_id: str) -> Path:
        return self._dir("accounts", account_id, "tmp")

    def _dir(self, *parts: str) -> Path:
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path
