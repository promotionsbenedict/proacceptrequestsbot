"""Smoke tests for the continuous approval streaming logic in bot.helper.

Telethon is mocked: we simulate a large pending queue that drains in
server-side chunks, plus FloodWaits and cancellation, and assert the helper
loops to completion and reports the real approved count.
"""

import asyncio

from telethon import errors

from bot.helper import HelperUserbot


class FakeImporter:
    def __init__(self, user_id):
        self.user_id = user_id


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.access_hash = 1000 + user_id


class FakeImportersResult:
    def __init__(self, count, importers):
        self.count = count
        self.importers = importers
        self.users = [FakeUser(i.user_id) for i in importers]


def make_helper():
    h = HelperUserbot(1, "hash", "session")
    h._ready = True
    h._client = object()  # truthy; primitives are monkeypatched below
    return h


def test_approve_all_drains_large_queue_in_chunks():
    """20 000 pending, cleared 100 per HideAll call, must all be approved."""
    h = make_helper()
    state = {"remaining": 20_000, "flood_once": True}

    async def fake_resolve_peer(chat_id):
        return "peer"

    async def fake_count(peer):
        return state["remaining"]

    async def fake_hide_all(peer):
        if state["flood_once"]:
            state["flood_once"] = False
            raise errors.FloodWaitError(request=None)
        state["remaining"] = max(0, state["remaining"] - 100)

    h._resolve_peer = fake_resolve_peer
    h._count_requests = fake_count
    h._hide_all = fake_hide_all

    progress_calls = []

    async def progress(done, total):
        progress_calls.append((done, total))

    # Speed up: no real sleeping.
    async def no_sleep(*a, **k):
        return None

    import bot.helper as helper_mod

    orig_sleep = asyncio.sleep
    asyncio.sleep = no_sleep  # type: ignore
    helper_mod.HelperUserbot._sleep_cancellable = staticmethod(no_sleep)  # type: ignore
    try:
        result = asyncio.get_event_loop().run_until_complete(
            h.approve_all_stream("chat", progress=progress)
        )
    finally:
        asyncio.sleep = orig_sleep  # type: ignore

    assert result.approved == 20_000, result.approved
    assert not result.cancelled
    assert state["remaining"] == 0
    # Progress must have been reported and be monotonic up to the total.
    assert progress_calls[-1][0] == 20_000


def test_approve_all_cancellation_stops_early():
    h = make_helper()
    state = {"remaining": 5_000, "cancel": False}

    async def fake_resolve_peer(chat_id):
        return "peer"

    async def fake_count(peer):
        return state["remaining"]

    async def fake_hide_all(peer):
        state["remaining"] = max(0, state["remaining"] - 100)
        if state["remaining"] <= 4_700:
            state["cancel"] = True  # trigger cancel after a few batches

    h._resolve_peer = fake_resolve_peer
    h._count_requests = fake_count
    h._hide_all = fake_hide_all

    async def no_sleep(*a, **k):
        return None

    orig_sleep = asyncio.sleep
    asyncio.sleep = no_sleep  # type: ignore
    try:
        result = asyncio.get_event_loop().run_until_complete(
            h.approve_all_stream("chat", cancelled=lambda: state["cancel"])
        )
    finally:
        asyncio.sleep = orig_sleep  # type: ignore

    assert result.cancelled
    assert 0 < result.approved < 5_000


def test_approve_amount_hits_exact_target():
    h = make_helper()
    # 1000 available, ask for 250 -> exactly 250.
    pool = list(range(1, 1001))

    async def fake_resolve_peer(chat_id):
        return "peer"

    async def fake_fetch(peer, limit):
        chunk = pool[:limit]
        return FakeImportersResult(len(pool), [FakeImporter(u) for u in chunk])

    async def fake_hide_one(peer, target):
        # target may be InputUser or raw id; pop the matching front item.
        uid = getattr(target, "user_id", target)
        if isinstance(target, int):
            uid = target
        else:
            uid = getattr(target, "user_id", None)
        # Just drain from the front regardless of exact id.
        if pool:
            pool.pop(0)

    h._resolve_peer = fake_resolve_peer
    h._fetch_requests = fake_fetch
    h._hide_one = fake_hide_one

    result = asyncio.get_event_loop().run_until_complete(
        h.approve_amount_stream("chat", 250)
    )
    assert result.approved == 250, result.approved
    assert len(pool) == 750


if __name__ == "__main__":
    test_approve_all_drains_large_queue_in_chunks()
    test_approve_all_cancellation_stops_early()
    test_approve_amount_hits_exact_target()
    print("OK: all streaming approval tests passed")
