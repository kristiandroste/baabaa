"""In-process publish/subscribe for live updates to every open window (browser or terminal).

Each window holds one subscription and receives its account's events over one SSE stream. Events
marked `owners=True` (model installs, the GPU queue) also go to every owner.
"""

import asyncio
import itertools

MAX_BACKLOG = 2000


class Subscription:
    def __init__(self, bus, account_id: str, is_owner: bool, window: str):
        self.bus = bus
        self.account_id = account_id
        self.is_owner = is_owner
        self.window = window
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def put(self, event: dict) -> None:
        if self.closed:
            return
        if self.queue.qsize() >= MAX_BACKLOG:
            # A window that stopped reading is dropped; it reconnects and reloads its state.
            self.closed = True
            self.queue.put_nowait(None)
            return
        self.queue.put_nowait(event)

    async def get(self, timeout: float):
        return await asyncio.wait_for(self.queue.get(), timeout)

    def close(self) -> None:
        self.closed = True
        self.bus.unsubscribe(self)


class EventBus:
    def __init__(self):
        self.subs: set[Subscription] = set()
        self._seq = itertools.count(1)

    def subscribe(self, account_id: str, is_owner: bool, window: str) -> Subscription:
        sub = Subscription(self, account_id, is_owner, window)
        self.subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self.subs.discard(sub)

    def publish(self, account_id: str | None, type: str, data: dict, owners: bool = False) -> None:
        event = {"seq": next(self._seq), "type": type, "data": data}
        for sub in list(self.subs):
            if (account_id is not None and sub.account_id == account_id) or (owners and sub.is_owner):
                sub.put(event)

    def broadcast(self, type: str, data: dict, exclude: str | None = None) -> None:
        event = {"seq": next(self._seq), "type": type, "data": data}
        for sub in list(self.subs):
            if exclude is None or sub.account_id != exclude:
                sub.put(event)

    def windows(self, account_id: str) -> int:
        return sum(1 for s in self.subs if s.account_id == account_id)
