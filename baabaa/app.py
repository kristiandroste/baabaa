"""The application container: every long-lived service, created once and shared by both windows."""

import asyncio
import os
import secrets

from .accounts import Accounts
from .agent.checkpoints import Checkpoints
from .agent.loop import Agent
from .agent.shell import ShellRunner
from .events import EventBus
from .gateway import Gateway
from .gpu import GPU
from .maindb import MainDB
from .models import ModelJobs, ModelRegistry
from .ollama import Ollama, OllamaError
from .paths import Paths
from .stats import Stats
from .store import Stores


class App:
    def __init__(self, paths: Paths | None = None, ollama_url: str | None = None):
        self.paths = paths or Paths()
        self.maindb = MainDB(self.paths.main_db)
        self.accounts = Accounts(self.maindb)
        self.accounts.worktree_root = lambda account_id: str(self.paths.account(account_id) / "worktrees")
        self.stats = Stats(self.paths.stats_db)
        self.gpu = GPU()
        url = ollama_url or os.environ.get("BAABAA_OLLAMA_URL") or self.maindb.get_setting("ollama_url") or "http://127.0.0.1:11434"
        self.ollama = Ollama(url)
        self.events = EventBus()
        self.registry = ModelRegistry(self.maindb, self.ollama, self.events)
        from .llamacpp import LlamaCppManager
        from .sdcpp import SdManager
        self.llamacpp = LlamaCppManager(self.paths, self.gpu)
        self.sdcpp = SdManager(self.paths, self.gpu)
        self.gateway = Gateway(self.ollama, self.stats, self.gpu, self.registry,
                               keep_alive=self.maindb.get_setting("keep_alive", "30m"), on_queue_change=self._queue_changed,
                               llamacpp=self.llamacpp, sdcpp=self.sdcpp)
        self.jobs = ModelJobs(self.registry, self.gateway, self.ollama, self.maindb, self.stats, self.events, self.gpu)
        self.stores = Stores(self.paths)
        self.shells = ShellRunner(self.paths)
        self.checkpoints = Checkpoints(self.paths)
        self.agent = Agent(self)
        from .mcp import MCPManager
        from .extend import Extensions
        self.mcp = MCPManager(self)
        self.extend = Extensions(self.paths)
        from .knowledge import Knowledge
        from .scheduler import Scheduler
        self.knowledge = Knowledge(self)
        self.scheduler = Scheduler(self)
        from .restarter import Restarter, UpdateWatch
        from .util import now_ms
        self.started_ms = now_ms()
        self.restarter = Restarter(self)
        self.updates = UpdateWatch(self)
        self.local_key = self._local_key()
        self.ollama_ok = None
        self.web = None  # the HTTP side (server/api.py), once serve() makes it

    def _local_key(self) -> str:
        """A secret the terminal client presents over the Unix socket. Sandboxed tools cannot read it."""
        path = self.paths.root / "local.key"
        if not path.exists():
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
        return path.read_text().strip()

    def _queue_changed(self, snap: dict) -> None:
        """Everyone sees whether the GPU is busy and with which model; only the account whose work it is sees its
        label (a short description of that work)."""
        running = snap.get("running")
        data = {"busy": running is not None,
                "running": {"kind": running["kind"], "model": running["model"], "label": ""} if running else None,
                "waiting": len(snap.get("waiting") or []), "paused": snap.get("paused")}
        owner = running.get("account_id") if running else None
        self.events.broadcast("queue", data, exclude=owner if running and running.get("label") else None)
        if owner and running.get("label"):
            self.events.publish(owner, "queue", {**data, "running": {**data["running"], "label": running["label"]}})

    def ollama_ram_caps(self) -> list[str] | None:
        """Ollama service settings still needed to cap its RAM caches (ollama.py); cached a minute."""
        import time
        from .ollama import ram_caps_missing, service_environment
        now = time.monotonic()
        cached = getattr(self, "_ollama_caps", None)
        if cached is None or now - cached[0] > 60:
            cached = (now, ram_caps_missing(service_environment()))
            self._ollama_caps = cached
        return cached[1]

    def gpu_others(self) -> list[dict]:
        """Programs other than baabaa's model servers holding GPU memory (for the status line)."""
        mine = set()
        for mgr in (self.llamacpp, self.sdcpp):
            if mgr.current is not None and mgr.current.proc is not None:
                mine.add(mgr.current.proc.pid)
        out = []
        from .system import process_name
        for p in self.gpu.processes() or []:
            comm = process_name(p["pid"])
            if p["pid"] in mine or comm.startswith("ollama"):
                continue
            out.append({"pid": p["pid"], "name": comm, "used": p["used"]})
        return out

    async def startup(self) -> None:
        try:
            version = await self.ollama.version()
            await self.registry.sync()
            self.ollama_ok = version
        except (OllamaError, OSError, asyncio.TimeoutError) as exc:
            self.ollama_ok = None
            print(f"baabaa: Ollama is not reachable ({exc}); models will appear once it is running.")
        paused = self.maindb.get_setting("gpu_paused")
        if paused:
            self.gateway.queue.pause(paused)
        import shutil
        for account in self.accounts.list():
            store = self.stores.get(account["id"])
            store.mark_interrupted()
            store.purge_incognito()
            scratch = self.paths.account(account["id"]) / "files" / "scratch"
            if scratch.is_dir():  # folders of chats that no longer exist
                for d in scratch.iterdir():
                    if d.is_dir() and store.conversation(d.name) is None:
                        shutil.rmtree(d, ignore_errors=True)
        self.knowledge.resume()
        self.scheduler.start()

    async def shutdown(self) -> None:
        self.scheduler.stop()
        self.shells.kill_all()
        await self.mcp.close_all()
        await self.llamacpp.stop()
        await self.sdcpp.stop()
        for turn in list(self.agent.turns.values()):
            if turn.task:
                turn.task.cancel()
