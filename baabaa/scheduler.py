"""Scheduled tasks: prompts that run by themselves, once or on a schedule, in the host's local time.

A schedule is {"kind": "once" | "daily" | "weekdays" | "weekly" | "interval", "at": "HH:MM",
"date": "YYYY-MM-DD" (once), "days": [0..6] (weekly; 0 is Monday), "every_min": N (interval, at least 15)}.
Each run starts a new conversation (or continues the schedule's last one), sends the prompt, and the
reply arrives like any other, with a notification in the browser. Runs missed while the server was off
happen once when it starts again.
"""

import asyncio
import datetime as dt
import re
import time

from .util import clip, now_ms

KINDS = ("once", "daily", "weekdays", "weekly", "interval")
TICK_S = 20


def validate(spec: dict) -> dict:
    kind = spec.get("kind")
    if kind not in KINDS:
        raise ValueError(f"The schedule's kind must be one of {', '.join(KINDS)}")
    out = {"kind": kind}
    if kind != "interval":
        at = str(spec.get("at") or "")
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at):
            raise ValueError("Give the time as HH:MM (24-hour)")
        out["at"] = at
    if kind == "once":
        try:
            dt.date.fromisoformat(str(spec.get("date") or ""))
        except ValueError as exc:
            raise ValueError("Give the date as YYYY-MM-DD") from exc
        out["date"] = spec["date"]
    if kind == "weekly":
        days = sorted({int(d) for d in (spec.get("days") or []) if str(d).isdigit() and 0 <= int(d) <= 6})
        if not days:
            raise ValueError("Choose at least one day of the week")
        out["days"] = days
    if kind == "interval":
        every = int(spec.get("every_min") or 0)
        if every < 15:
            raise ValueError("Run at most every 15 minutes")
        out["every_min"] = every
    return out


def next_run(spec: dict, after_ms: int, last_ms: int | None = None) -> int | None:
    """The next run strictly after `after_ms` (local time), or None when there is none."""
    kind = spec["kind"]
    after = dt.datetime.fromtimestamp(after_ms / 1000)
    if kind == "interval":
        base = last_ms or after_ms
        step = spec["every_min"] * 60_000
        n = max(1, -(-(after_ms - base) // step))
        return base + n * step
    hh, mm = (int(x) for x in spec["at"].split(":"))
    if kind == "once":
        when = dt.datetime.combine(dt.date.fromisoformat(spec["date"]), dt.time(hh, mm))
        return int(when.timestamp() * 1000) if when > after else None
    for i in range(0, 8):
        day = after.date() + dt.timedelta(days=i)
        when = dt.datetime.combine(day, dt.time(hh, mm))
        if when <= after:
            continue
        wd = day.weekday()
        if kind == "daily" or (kind == "weekdays" and wd < 5) or (kind == "weekly" and wd in spec["days"]):
            return int(when.timestamp() * 1000)
    return None


def describe(spec: dict) -> str:
    kind = spec["kind"]
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if kind == "once":
        return f"Once, {spec['date']} at {spec['at']}"
    if kind == "daily":
        return f"Every day at {spec['at']}"
    if kind == "weekdays":
        return f"Weekdays at {spec['at']}"
    if kind == "weekly":
        return f"{', '.join(names[d] for d in spec['days'])} at {spec['at']}"
    h, m = divmod(spec["every_min"], 60)
    return f"Every {f'{h} h ' if h else ''}{f'{m} min' if m else ''}".strip()


class Scheduler:
    def __init__(self, app):
        self.app = app
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.get_running_loop().create_task(self._loop())

    def stop(self) -> None:
        if self.task:
            self.task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # a broken schedule must not stop the others
                import traceback
                traceback.print_exc()
            await asyncio.sleep(TICK_S)

    async def tick(self, now: int | None = None) -> int:
        if self.app.restarter.draining:  # a restart is waiting for running replies: due tasks run after it
            return 0
        now = now or now_ms()
        ran = 0
        for account in self.app.accounts.list():
            if account.get("disabled"):
                continue
            store = self.app.stores.get(account["id"])
            for sch in store.due_schedules(now):
                await self.run(account, sch, now)
                ran += 1
        return ran

    async def run(self, account: dict, sch: dict, now: int | None = None, manual: bool = False) -> dict:
        now = now or now_ms()
        store = self.app.stores.get(account["id"])
        settings = sch["settings"]
        conv = None
        if settings.get("same_conversation") and sch.get("last_conv"):
            conv = store.conversation(sch["last_conv"])
        if conv is None:
            stamp = time.strftime("%d %b %H:%M", time.localtime(now / 1000))
            project_id = settings.get("project_id") if settings.get("project_id") and store.project(settings["project_id"]) else None
            model = settings.get("model") if settings.get("model") in [m["name"] for m in self.app.registry.approved("chat")] else None
            conv = store.create_conversation(title=clip(f"{sch['title']} · {stamp}", 200), model=model or self.app.registry.default_chat(),
                                             mode="manual", project_id=project_id,
                                             settings={"think": settings["think"]} if settings.get("think") else {})
        status = "started"
        try:
            await self.app.agent.send(account, conv["id"], sch["prompt"], [], "schedule", research=bool(settings.get("research")))
        except (KeyError, ValueError, RuntimeError) as exc:
            status = f"error: {exc}"
        once = sch["spec"]["kind"] == "once"
        nxt = None if once or (manual and not sch["enabled"]) else next_run(sch["spec"], now, now)
        fields = {"last_ms": now, "last_conv": conv["id"], "last_status": status, "runs": sch.get("runs", 0) + 1}
        if not manual or once:
            fields["next_ms"] = nxt
            if nxt is None:
                fields["enabled"] = False  # a one-off that has run
        sch = store.update_schedule(sch["id"], **fields)
        self.app.stats.event("schedule_run", account["id"], conv["id"], "schedule", manual=manual, kind=sch["spec"]["kind"],
                             research=bool(settings.get("research")))
        self.app.events.publish(account["id"], "schedule", {"schedule": sch})
        turn = self.app.agent.turns.get(conv["id"]) if status == "started" else None
        if turn is not None and getattr(turn, "task", None) is not None:
            asyncio.get_running_loop().create_task(self._outcome(account, sch["id"], conv["id"], turn.task))
        return sch

    async def _outcome(self, account: dict, sid: str, conv_id: str, task) -> None:
        """When the run's turn ends, record how it ended (the reply's status) in place of 'started'."""
        await asyncio.wait({task})
        store = self.app.stores.get(account["id"])
        conv = store.conversation(conv_id)
        msg = store.message(conv["leaf_id"]) if conv and conv.get("leaf_id") else None
        sch = store.schedule(sid)
        if sch is None or sch.get("last_conv") != conv_id:
            return  # deleted, or a newer run has started since
        sch = store.update_schedule(sid, last_status=(msg or {}).get("status") or "done")
        self.app.events.publish(account["id"], "schedule", {"schedule": sch})
