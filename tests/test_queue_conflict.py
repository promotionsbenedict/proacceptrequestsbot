"""Regression tests: HideAll "join request was missing or already handled".

Telegram can raise HIDE_REQUESTER_MISSING mid-bulk-approval after part of the
batch landed. approve_all_stream must recount, credit only verified queue
decreases, keep going while the queue shrinks, stop when it doesn't, and still
treat permission / unrelated errors as fatal.
"""

import asyncio

import pytest
from telethon import errors

from bot.helper import MAX_CONFLICT_STALLS, HelperUserbot


def conflict_error():
    cls = getattr(errors, "HideRequesterMissingError", None)
    if cls is not None:
        return cls(request=None)
    return Exception("The join request was missing or was already handled")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(*_a, **_k):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(HelperUserbot, "_sleep_cancellable", staticmethod(fast_sleep))


def make_helper(state, hide_all):
    h = HelperUserbot(1, "hash", "session")
    h._ready = True
    h._client = object()

    async def resolve_peer(_chat_id):
        return "peer"

    async def count(_peer):
        state["counts"] += 1
        return state["remaining"]

    async def hide(peer):
        state["hides"] += 1
        await hide_all(peer)

    h._resolve_peer = resolve_peer
    h._count_requests = count
    h._hide_all = hide
    return h


def new_state(remaining):
    return {"remaining": remaining, "hides": 0, "counts": 0}


def test_conflict_detection_matches_message_and_class():
    assert HelperUserbot._is_queue_conflict(conflict_error())
    assert HelperUserbot._is_queue_conflict(
        Exception("The join request was missing or was already handled (caused by X)")
    )
    assert not HelperUserbot._is_queue_conflict(errors.ChatAdminRequiredError(request=None))
    assert not HelperUserbot._is_queue_conflict(Exception("CHANNEL_INVALID"))


def test_partial_approval_then_conflict_continues_to_zero():
    state = new_state(500)

    async def hide_all(_peer):
        # Every call approves 60, but every other call also reports a conflict.
        state["remaining"] = max(0, state["remaining"] - 60)
        if state["hides"] % 2 == 0:
            raise conflict_error()

    h = make_helper(state, hide_all)
    progress = []

    async def on_progress(done, total):
        progress.append((done, total))

    result = asyncio.run(h.approve_all_stream("chat", progress=on_progress))

    assert not result.note
    assert result.approved == 500
    assert state["remaining"] == 0
    assert progress[-1][0] == 500
    done_values = [d for d, _ in progress]
    assert done_values == sorted(done_values)


def test_conflict_without_progress_stops_with_accurate_count():
    state = new_state(300)

    async def hide_all(_peer):
        # First call genuinely approves 100, then Telegram keeps conflicting.
        if state["hides"] == 1:
            state["remaining"] -= 100
            return
        raise conflict_error()

    h = make_helper(state, hide_all)
    result = asyncio.run(h.approve_all_stream("chat"))

    assert result.approved == 100
    assert "already handled" in result.note
    assert "200" in result.note
    # Bounded: 1 successful call + MAX_CONFLICT_STALLS conflicting calls.
    assert state["hides"] == 1 + MAX_CONFLICT_STALLS


def test_permission_failure_is_not_retried():
    state = new_state(400)

    async def hide_all(_peer):
        if state["hides"] == 1:
            state["remaining"] -= 100
            return
        raise errors.ChatAdminRequiredError(request=None)

    h = make_helper(state, hide_all)
    result = asyncio.run(h.approve_all_stream("chat"))

    assert result.approved == 100
    assert result.note == h._manual_text()
    assert state["hides"] == 2


def test_unrelated_error_is_not_retried():
    state = new_state(400)

    async def hide_all(_peer):
        raise errors.ChannelInvalidError(request=None)

    h = make_helper(state, hide_all)
    result = asyncio.run(h.approve_all_stream("chat"))

    assert result.approved == 0
    assert result.note == "Telegram returned an error while approving."
    assert state["hides"] == 1
