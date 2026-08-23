"""Event bus semantics: atomic subscribe+snapshot, no duplicates/losses.

Regression guard for the SSE backlog race: subscribe-then-read-recent could
deliver one event twice (backlog + queue) or lose it (published after
snapshot check, before queue registration was visible).
"""
from __future__ import annotations

import asyncio

from calliope.events.bus import EventBus


async def _drive(bus: EventBus, n_pub: int, during_subscribe=None):
    """Publish n_pub events; `during_subscribe` (if set) runs concurrently
    with the subscribe call to exercise the race window."""

    async def publisher():
        for i in range(n_pub):
            await bus.publish("tick", {"i": i})
            await asyncio.sleep(0)

    if during_subscribe is not None:
        # Interleave publishes with the subscribe to hit the race window.
        sub_task = asyncio.create_task(during_subscribe())
        pub_task = asyncio.create_task(publisher())
        result = await sub_task
        await pub_task
        return result
    await publisher()
    return None


def test_subscribe_snapshot_no_duplicates_no_loss():
    """Events published while subscribing land in exactly one of snapshot/queue."""
    bus = EventBus()
    # Pre-fill backlog (indices 0-4).
    asyncio.run(_drive(bus, 5))

    async def scenario():
        # Concurrent publisher uses disjoint indices 100-109.
        async def publisher():
            for i in range(100, 110):
                await bus.publish("tick", {"i": i})
                await asyncio.sleep(0)

        async def do_subscribe():
            return await bus.subscribe(backlog=20)

        sub_task = asyncio.create_task(do_subscribe())
        pub_task = asyncio.create_task(publisher())
        q, snapshot = await sub_task
        await pub_task

        # Drain the queue.
        queued = []
        while not q.empty():
            queued.append(q.get_nowait())

        seen = [e["data"]["i"] for e in snapshot] + [e["data"]["i"] for e in queued]
        # All 5 pre-filled + all 10 concurrent events seen exactly once.
        expected = set(range(5)) | set(range(100, 110))
        assert set(seen) == expected
        assert len(seen) == len(set(seen)) == 15

    asyncio.run(scenario())


def test_subscribe_without_backlog_returns_queue_only():
    bus = EventBus()
    asyncio.run(_drive(bus, 3))

    async def scenario():
        q = await bus.subscribe()  # no backlog
        assert isinstance(q, asyncio.Queue)
        assert q.empty()  # nothing replayed

    asyncio.run(scenario())


def test_full_queue_drops_subscriber():
    """A subscriber that never drains is evicted on QueueFull, publish survives."""
    bus = EventBus()

    async def scenario():
        q = await bus.subscribe()
        for i in range(300):  # overflows the 256 maxsize
            await bus.publish("tick", {"i": i})
        assert q not in bus._subscribers  # evicted
        # A fresh subscriber still receives events.
        q2 = await bus.subscribe()
        await bus.publish("after", {})
        assert not q2.empty()

    asyncio.run(scenario())


def test_backlog_snapshot_respects_limit():
    bus = EventBus()
    asyncio.run(_drive(bus, 50))

    async def scenario():
        q, snapshot = await bus.subscribe(backlog=10)
        assert len(snapshot) == 10
        assert snapshot[-1]["data"]["i"] == 49  # newest end
        assert q.empty()

    asyncio.run(scenario())
