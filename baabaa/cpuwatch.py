"""The CPU guard. Model work belongs on the GPU: on the CPU it makes a small computer strain (fans at full
speed, heat) and takes far longer. While a request runs, baabaa samples the CPU time of the program serving
it. A model that is fully on the GPU keeps about one core busy (the thread that feeds the GPU and waits for
it). If the program keeps more than `LIMIT_CORES` busy for `WINDOW_S` seconds, model work is running on
the CPU, and the request is stopped along with the program.

The readings (CPU seconds and the busiest moment) go into the statistics with each request.
"""

import asyncio
import os
import time

from .system import cpu_seconds, processes

LIMIT_CORES = 2.5
WINDOW_S = 10.0
INTERVAL_S = 1.0


def ollama_runner_pids(model_file: str | None = None) -> list[int]:
    """Ollama's model processes: the children of `ollama serve`, which do the computing. With `model_file`,
    only the one serving that file."""
    procs = processes()
    serve = {p["pid"] for p in procs if len(p["argv"]) >= 2 and os.path.basename(p["argv"][0]) == b"ollama"
             and p["argv"][1] == b"serve"}
    kids = [p for p in procs if p["ppid"] in serve]
    if model_file:
        wanted = model_file.encode()
        mine = [p["pid"] for p in kids if any(wanted in a for a in p["argv"])]
        return mine or [p["pid"] for p in kids]
    return [p["pid"] for p in kids]


class CpuWatch:
    """Samples `pids()` every `interval` seconds while a request runs; calls `on_trip(reason)` once when
    the processes stay above `limit` cores for `window` seconds."""

    def __init__(self, pids, on_trip, limit: float = LIMIT_CORES, window: float = WINDOW_S, interval: float = INTERVAL_S):
        self.pids, self.on_trip = pids, on_trip
        self.limit, self.window, self.interval = limit, window, interval
        self.cpu_s = 0.0
        self.peak = 0.0
        self.tripped: str | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> "CpuWatch":
        self._task = asyncio.get_running_loop().create_task(self._run())
        return self

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - the watch must never break a request
                pass

    async def _run(self) -> None:
        pids = self.pids()
        last_t, last_c = time.monotonic(), cpu_seconds(pids)
        hot_since = None
        while True:
            await asyncio.sleep(self.interval)
            now = time.monotonic()
            c = cpu_seconds(pids)
            if c is None or last_c is None:  # not started yet, or restarted: look again
                pids = self.pids()
                last_t, last_c = now, cpu_seconds(pids)
                continue
            used = max(0.0, c - last_c)
            self.cpu_s += used
            cores = used / max(now - last_t, 1e-6)
            self.peak = max(self.peak, cores)
            if cores > self.limit:
                hot_since = hot_since or last_t
                if now - hot_since >= self.window:
                    self.tripped = (f"the model's program kept {cores:.1f} CPU cores busy for {now - hot_since:.0f} s, "
                                    "so model work was running on the CPU; baabaa stopped it")
                    try:
                        await self.on_trip(self.tripped)
                    except Exception:  # noqa: BLE001 - stopping is best effort; the request fails regardless
                        pass
                    return
            else:
                hot_since = None
            last_t, last_c = now, c
