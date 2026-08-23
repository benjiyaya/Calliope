"""In-process SSE event bus."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._lock = asyncio.Lock()
        self.recent: list[dict[str, Any]] = []

    async def subscribe(
        self, backlog: int = 0
    ) -> asyncio.Queue[dict[str, Any]] | tuple[asyncio.Queue[dict[str, Any]], list[dict[str, Any]]]:
        """Subscribe to future events.

        With `backlog > 0`, returns (queue, snapshot) taken atomically under
        the publish lock — no event can appear in both the snapshot and the
        queue (which would deliver it twice), and none is lost in between.
        """
        async with self._lock:
            q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
            self._subscribers.append(q)
            if backlog > 0:
                return q, list(self.recent[-backlog:])
            return q

    async def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    async def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        event = {
            "type": event_type,
            "data": data or {},
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        # recent-append and queue delivery share the lock with subscribe()
        # so backlog snapshots are consistent (no duplicate/lost events).
        async with self._lock:
            self.recent.append(event)
            if len(self.recent) > 200:
                self.recent = self.recent[-200:]
            dead: list[asyncio.Queue[dict[str, Any]]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    def format_sse(self, event: dict[str, Any]) -> str:
        return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


event_bus = EventBus()
