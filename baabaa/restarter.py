"""Restarting the server in place: to run an update, to pick up changed files, or because someone asked.

A restart waits for running replies and model jobs (installs, fit tests) to finish, unless asked to go
now. While it waits, new replies are refused with a message that says why, and scheduled tasks wait for
the restart. The code it restarts into must pass its start-up check (`baabaa selfcheck`) first. Then the
server shuts down as on Ctrl+C and replaces itself with that code (os.execve: the same process, so a
terminal, `baabaa start` and systemd all keep track of it). If an installed update then fails to start,
the new process switches back to the version it came from and starts that (see `fallback` in
__main__.serve).

Who may restart: every account, or only owners (the owner's setting "restart_by"); the local terminal always.
"""

import asyncio
import os
import sys

from . import __version__, update
from .util import now_ms

RECENT_MS = 5 * 60_000   # a member cannot restart baabaa again this soon after it started


class Restarter:
    def __init__(self, app):
        self.app = app
        self.pending: dict | None = None
        self.error: str | None = None
        self.exec_root = None
        self._wake = asyncio.Event()

    # who and what ------------------------------------------------------------------------------------
    def allowed(self, account: dict | None) -> bool:
        if account is None:  # the local terminal
            return True
        return account["role"] == "owner" or self.app.maindb.get_setting("restart_by", "all") == "all"

    @property
    def draining(self) -> bool:
        return self.pending is not None

    def refuse_new_work(self) -> None:
        """Called before a reply starts: refuses it while a restart waits for the running ones."""
        if self.pending is not None:
            raise RuntimeError("baabaa is about to restart and is waiting for the replies already running. "
                               "Send this again in a minute.")

    def busy(self, viewer: str | None = None) -> dict:
        """Running replies (titles only for the viewer's own conversations) and model jobs."""
        replies, yours = 0, []
        for conv_id, turn in list(self.app.agent.turns.items()):
            replies += 1
            if viewer and turn.account["id"] == viewer:
                conv = self.app.stores.get(viewer).conversation(conv_id)
                yours.append((conv or {}).get("title") or "Untitled")
        jobs = []
        for job_id in list(self.app.jobs.tasks):
            j = self.app.maindb.job_get(job_id) or {}
            jobs.append({"kind": j.get("kind"), "model": (j.get("params") or {}).get("model")})
        return {"replies": replies, "yours": yours, "jobs": jobs}

    def _busy_now(self) -> bool:
        return bool(self.app.agent.turns) or bool(self.app.jobs.tasks)

    def public(self) -> dict | None:
        if self.pending is None:
            return None
        return {k: self.pending.get(k) for k in ("by", "reason", "now", "since_ms", "target", "phase", "next_url")}

    # asking -------------------------------------------------------------------------------------------
    def request(self, account: dict | None, reason: str, now: bool = False) -> dict:
        """reason: "update" (a new version), "changed" (files changed on disk), "network" (the owner changed who
        can connect) or "restart". `account` is who asked (None: the local terminal)."""
        if account is not None and account["role"] != "owner" and now_ms() - self.app.started_ms < RECENT_MS and self.pending is None:
            raise RuntimeError("baabaa restarted a few minutes ago. Try again in a few minutes, or ask an owner.")
        root = update.target_root()
        target = update._tree_version(root) or __version__
        by = account["display_name"] if account else "the terminal"
        web = getattr(self.app, "web", None)
        next_urls = web.network_state()["next_urls"] if web is not None else None
        if self.pending is None:
            self.pending = {"by": by, "reason": reason, "now": bool(now), "since_ms": now_ms(), "target": target,
                            "phase": "waiting", "next_url": next_urls[0] if next_urls else None}
        else:  # asking again: "now" wins, and the reason gets the more specific one
            self.pending["now"] = self.pending["now"] or bool(now)
            if reason in ("update", "network"):
                self.pending["reason"] = reason
            self.pending["target"] = target
            self.pending["next_url"] = next_urls[0] if next_urls else None
        self.error = None
        self.app.stats.event("restart_requested", account["id"] if account else None, window="server" if account is None else None,
                             reason=reason, now=bool(now), running=__version__, target=target)
        self._announce()
        self._wake.set()
        return self.public()

    def cancel(self, account: dict | None) -> None:
        if self.pending is None or self.pending.get("phase") == "restarting":
            return
        self.pending = None
        self.app.stats.event("restart_cancelled", account["id"] if account else None)
        self._announce()

    def _announce(self) -> None:
        self.app.events.broadcast("restart", {"pending": self.public(), "error": self.error})

    # doing ---------------------------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        """The server's restart task: waits for a request and for the work to finish, then stops the server
        with `exec_root` set, which tells serve() to replace the process."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.pending is not None and not self.pending["now"] and self._busy_now():
                try:
                    await asyncio.wait_for(self._wake.wait(), 1.0)
                    self._wake.clear()
                except asyncio.TimeoutError:
                    pass
            if self.pending is None:
                continue
            root = update.target_root()
            ok, out = await asyncio.to_thread(update.selfcheck, root)
            if not ok:
                self.error = f"The new code did not pass its start-up check, so baabaa kept running as it was:\n{out}"
                self.app.stats.event("restart_failed", None, window="server", stage="selfcheck")
                self.pending = None
                self._announce()
                continue
            self.pending["phase"] = "restarting"
            self.exec_root = root
            self._announce()
            await asyncio.sleep(0.5)  # let the windows hear it before the connections close
            stop.set()
            return


def exec_args(argv: list[str], root) -> list[str]:
    """The command line that restarts this server: the same interpreter and arguments, the code in `root`."""
    from . import BOOT
    return [sys.executable, "-c", BOOT, str(root), *argv]


def exec_env(root, fallback: str | None) -> dict:
    env = dict(os.environ)
    paths = [str(root)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and p != str(root)
                           and not p.startswith(str(update.prefix()))]
    env["PYTHONPATH"] = os.pathsep.join(paths)
    if fallback:
        env["BAABAA_RESTART_FALLBACK"] = fallback
    else:
        env.pop("BAABAA_RESTART_FALLBACK", None)
    return env


class UpdateWatch:
    """The server's side of updates: for an installed copy, a check of the release list once a day (when
    checks are on); for any other copy, whether the code on disk changed since the server started. Windows
    hear an "update" event whenever what they should show changes, and fetch GET /api/update."""

    def __init__(self, app):
        self.app = app
        self.kind = update.kind()
        self.started = update.fingerprint() if self.kind != "installed" else None
        self.changed = False
        self.busy: str | None = None   # "checking" or "downloading"
        self.error: str | None = None
        self._last = None

    def status(self) -> dict:
        st = update.status(__version__, self.changed)
        st["busy"] = self.busy
        if self.error:
            st["error"] = self.error
        return st

    async def run(self) -> None:
        import traceback
        await asyncio.sleep(20)  # after start-up
        while True:
            try:
                if self.kind != "installed" and not self.changed:
                    self.changed = await asyncio.to_thread(update.fingerprint) != self.started
                if self._check_due():
                    await self.check(None)
                self._announce()
            except Exception:
                traceback.print_exc()
            await asyncio.sleep(60)

    def _check_due(self) -> bool:
        if self.kind != "installed" or update.disabled() or os.environ.get("BAABAA_DISABLE_AUTOUPDATER") == "1":
            return False
        state = update.load_state()
        return bool(state.get("checks", True)) and now_ms() - int(state.get("checked_ms") or 0) > update.CHECK_EVERY_S * 1000

    def _announce(self, force: bool = False) -> None:
        st = self.status()
        key = (st["state"], st.get("target"), st.get("error"), st.get("busy"), st.get("checked_ms"))
        if force or key != self._last:
            self._last = key
            self.app.events.broadcast("update", {"state": st["state"], "target": st.get("target"), "busy": st.get("busy")})

    async def check(self, account: dict | None) -> dict:
        if self.busy:
            return self.status()
        self.busy, self.error = "checking", None
        self._announce()
        try:
            state = await asyncio.to_thread(update.check)
        finally:
            self.busy = None
        latest = (state.get("latest") or {}).get("version")
        self.app.stats.event("update_check", account["id"] if account else None, manual=account is not None,
                             ok=not state.get("error"), latest=latest, running=__version__,
                             staged=state.get("staged"), published=state.get("published"))
        self._announce(force=True)
        return self.status()

    async def install(self, account: dict | None, now: bool = False) -> dict:
        """Install what the status offers (downloading it first if needed), then restart into it."""
        st = self.status()
        if st["state"] == "available":
            self.busy, self.error = "downloading", None
            self._announce()
            try:
                await asyncio.to_thread(update.fetch_version, st["target"])
            except update.UpdateError as exc:
                self.error = str(exc)
                raise RuntimeError(str(exc)) from None
            finally:
                self.busy = None
                self._announce()
            st = self.status()
        if st["state"] == "ready":
            await self.switch(account, st["target"], "update_apply")
            st = self.status()
        if st["state"] not in ("installed", "changed"):
            raise RuntimeError("There is no update to install.")
        return self.app.restarter.request(account, "update" if st["state"] == "installed" else "changed", now)

    async def switch(self, account: dict | None, version: str, event: str) -> None:
        await asyncio.to_thread(update.backup_databases, self.app.paths.root, __version__)
        before = await asyncio.to_thread(update.switch, version)
        self.app.stats.event(event, account["id"] if account else None, running=__version__, before=before, to=version)
        await asyncio.to_thread(update.cleanup, {__version__})
        self._announce(force=True)

    async def previous(self, account: dict | None, now: bool = False) -> dict:
        st = self.status()
        if not st.get("previous"):
            raise RuntimeError("There is no previous version on this computer.")
        await self.switch(account, st["previous"], "update_rollback")
        return self.app.restarter.request(account, "update", now)
