"""Running shell commands for the agent: always inside the sandbox, with a timeout, capped output,
and optional background shells whose output the model reads later."""

import asyncio
import os
import signal
import time

from .. import sandbox
from ..util import new_id

MAX_OUTPUT = 16000          # characters returned to the model per call (head and tail)
MAX_BACKGROUND = 8          # background shells per conversation


def cap_output(text: str, limit: int = MAX_OUTPUT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head, tail = text[: limit // 2], text[-limit // 2:]
    omitted = text[limit // 2: -limit // 2]
    lines = omitted.count("\n")
    return f"{head}\n\n… [{lines} lines / {len(omitted)} characters omitted; use grep, head or tail to see them] …\n\n{tail}", True


class ShellRunner:
    def __init__(self, paths):
        self.paths = paths
        self.background: dict[str, dict] = {}

    def _argv(self, account_id: str, command: str, folder: str, writable: list[str], readable: list[str], network: bool):
        home = str(self.paths.account_home(account_id))
        tmp = str(self.paths.account_tmp(account_id))
        rw = [folder] + [w for w in writable if w != folder] + [home, tmp]
        ro = [r for r in readable if r not in rw]
        argv = sandbox.helper_argv(["/bin/bash", "-c", command], folder, rw, ro, network)
        return argv, sandbox.sandbox_env(home, tmp, roots=[folder] + list(writable) + list(readable))

    async def run(self, account_id, command, folder, writable, readable, network, timeout=120, on_output=None):
        """Run to completion. Returns {'output', 'exit_code', 'timed_out', 'duration_ms', 'truncated', 'denied'}."""
        argv, env = self._argv(account_id, command, folder, writable, readable, network)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=folder, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        chunks, total, timed_out = [], 0, False

        async def pump():
            nonlocal total
            while True:
                data = await proc.stdout.read(8192)
                if not data:
                    break
                total += len(data)
                if total < 4 * 1024 * 1024:
                    chunks.append(data)
                if on_output:
                    on_output(data.decode("utf-8", "replace"))

        try:
            await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            _kill(proc)
            await proc.wait()
        except asyncio.CancelledError:
            _kill(proc)
            raise
        text = b"".join(chunks).decode("utf-8", "replace")
        if timed_out:
            text += f"\n[stopped after {timeout} s]"
        out, truncated = cap_output(text)
        denied = proc.returncode == 126 and "refusing to run unconfined" in text
        return {"output": out, "exit_code": proc.returncode, "timed_out": timed_out, "truncated": truncated,
                "duration_ms": round((time.monotonic() - started) * 1000), "denied": denied, "bytes": total}

    async def start_background(self, conv_id, account_id, command, folder, writable, readable, network) -> str:
        mine = [b for b in self.background.values() if b["conv_id"] == conv_id and b["proc"].returncode is None]
        if len(mine) >= MAX_BACKGROUND:
            raise RuntimeError(f"at most {MAX_BACKGROUND} background commands per conversation")
        argv, env = self._argv(account_id, command, folder, writable, readable, network)
        shell_id = "bg-" + new_id()[-8:]
        log_path = os.path.join(str(self.paths.account_tmp(account_id)), f"{shell_id}.log")
        log = open(log_path, "wb")
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=folder, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        log.close()
        self.background[shell_id] = {"id": shell_id, "conv_id": conv_id, "account_id": account_id, "command": command,
                                     "proc": proc, "log": log_path, "read": 0, "started": time.time()}
        return shell_id

    def read_background(self, shell_id: str, conv_id: str) -> dict:
        b = self.background.get(shell_id)
        if not b or b["conv_id"] != conv_id:
            raise KeyError(shell_id)
        with open(b["log"], "rb") as f:
            f.seek(b["read"])
            data = f.read(4 * 1024 * 1024)
        b["read"] += len(data)
        text, truncated = cap_output(data.decode("utf-8", "replace"))
        return {"id": shell_id, "command": b["command"], "running": b["proc"].returncode is None,
                "exit_code": b["proc"].returncode, "output": text, "truncated": truncated}

    def kill_background(self, shell_id: str, conv_id: str) -> bool:
        b = self.background.get(shell_id)
        if not b or b["conv_id"] != conv_id:
            raise KeyError(shell_id)
        if b["proc"].returncode is None:
            _kill(b["proc"])
            return True
        return False

    def list_background(self, conv_id: str) -> list[dict]:
        return [{"id": b["id"], "command": b["command"], "running": b["proc"].returncode is None,
                 "exit_code": b["proc"].returncode, "started": b["started"]}
                for b in self.background.values() if b["conv_id"] == conv_id]

    def kill_all(self, conv_id: str | None = None) -> None:
        for b in self.background.values():
            if (conv_id is None or b["conv_id"] == conv_id) and b["proc"].returncode is None:
                _kill(b["proc"])


def _kill(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
