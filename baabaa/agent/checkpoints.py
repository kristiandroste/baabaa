"""File checkpoints for rewind: content-addressed snapshots of a working folder, in pure Python.

The same idea as a shadow git repository (tree hashes, not commits) without needing git: each snapshot
is a manifest {relative path: [size, mtime_ns, mode, sha256]} and file contents are stored once by
hash. Unchanged files (same size and mtime) are not re-read. Large folders are skipped.
"""

import hashlib
import os
import shutil
import time
import zlib

from .tools import SKIP_DIRS

MAX_FILES = 20000
MAX_TOTAL = 300 * 2**20
MAX_FILE = 10 * 2**20


class TooLarge(Exception):
    pass


class Checkpoints:
    def __init__(self, paths):
        self.paths = paths

    def _blob_path(self, account_id: str, sha: str) -> str:
        d = self.paths.account_files(account_id, "blobs", sha[:2])
        return os.path.join(str(d), sha[2:])

    def snapshot(self, account_id: str, folder: str, previous: dict | None = None) -> dict:
        prev = (previous or {}).get("files", {})
        # Timestamps are coarse (a few ms), so a file rewritten right around the previous snapshot can keep
        # its size and mtime. Only trust cached hashes for files older than that snapshot by 2 s.
        trust_before = (previous or {}).get("taken_ns", 0) - 2_000_000_000
        taken_ns = time.time_ns()
        files, total, skipped = {}, 0, []
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                full = os.path.join(dirpath, name)
                relp = os.path.relpath(full, folder)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if not os.path.isfile(full) or os.path.islink(full):
                    continue
                if st.st_size > MAX_FILE:
                    skipped.append(relp)
                    continue
                total += st.st_size
                if len(files) >= MAX_FILES or total > MAX_TOTAL:
                    raise TooLarge(f"{folder} is too large to checkpoint")
                old = prev.get(relp)
                if old and old[0] == st.st_size and old[1] == st.st_mtime_ns and st.st_mtime_ns < trust_before:
                    files[relp] = old
                    continue
                try:
                    with open(full, "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                sha = hashlib.sha256(data).hexdigest()
                blob = self._blob_path(account_id, sha)
                if not os.path.exists(blob):
                    tmp = blob + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(zlib.compress(data, 3))
                    os.replace(tmp, blob)
                files[relp] = [st.st_size, st.st_mtime_ns, st.st_mode & 0o777, sha]
        return {"files": files, "skipped": skipped[:100], "taken_ns": taken_ns}

    def restore(self, account_id: str, folder: str, manifest: dict) -> dict:
        """Make the folder match `manifest`: rewrite changed files, recreate deleted ones, remove new ones."""
        target = manifest.get("files", {})
        current = self.snapshot(account_id, folder, None)["files"]  # hash everything: correctness over speed
        restored, removed = [], []
        for relp, (size, _mtime, mode, sha) in target.items():
            cur = current.get(relp)
            if cur and cur[3] == sha:
                continue
            with open(self._blob_path(account_id, sha), "rb") as f:
                data = zlib.decompress(f.read())
            full = os.path.join(folder, relp)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(data)
            os.chmod(full, mode)
            restored.append(relp)
        for relp in current:
            if relp not in target and relp not in manifest.get("skipped", []):
                try:
                    os.remove(os.path.join(folder, relp))
                    removed.append(relp)
                except OSError:
                    pass
        # remove folders left empty by the removals
        for relp in removed:
            d = os.path.dirname(os.path.join(folder, relp))
            while d.startswith(folder) and d != folder:
                try:
                    os.rmdir(d)
                except OSError:
                    break
                d = os.path.dirname(d)
        return {"restored": restored, "removed": removed}


def purge_blobs(paths, account_id: str) -> None:
    shutil.rmtree(str(paths.account_files(account_id, "blobs")), ignore_errors=True)
