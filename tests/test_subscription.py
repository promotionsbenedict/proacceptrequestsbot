"""Regression tests for bug #2: mandatory-subscription bypass.

The old ``is_subscribed`` returned ``True`` whenever ``get_chat_member`` raised,
so any user could bypass the gate when the bot could not verify membership.
Membership must now fail closed, and only real members/admins/owners pass.
"""

import asyncio
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest

from bot.middlewares import is_subscribed


class FakeBot:
    def __init__(self, *, member=None, error=None):
        self._member = member
        self._error = error

    async def get_chat_member(self, channel, user_id):
        if self._error is not None:
            raise self._error
        return self._member


def _err():
    return TelegramBadRequest(method=None, message="chat not found")


def test_fails_closed_when_membership_cannot_be_verified():
    async def run():
        bot = FakeBot(error=_err())
        assert await is_subscribed(bot, "@proacceptrequests", 42) is False

    asyncio.run(run())


def test_member_admin_owner_allowed():
    async def run():
        for status in ("member", "administrator", "creator"):
            bot = FakeBot(member=SimpleNamespace(status=status, is_member=True))
            assert await is_subscribed(bot, "@c", 1) is True

    asyncio.run(run())


def test_left_user_blocked():
    async def run():
        bot = FakeBot(member=SimpleNamespace(status="left", is_member=False))
        assert await is_subscribed(bot, "@c", 1) is False

    asyncio.run(run())


def test_kicked_user_blocked():
    async def run():
        bot = FakeBot(member=SimpleNamespace(status="kicked", is_member=False))
        assert await is_subscribed(bot, "@c", 1) is False

    asyncio.run(run())


def test_restricted_but_still_member_allowed():
    async def run():
        bot = FakeBot(member=SimpleNamespace(status="restricted", is_member=True))
        assert await is_subscribed(bot, "@c", 1) is True

    asyncio.run(run())


def test_restricted_and_gone_blocked():
    async def run():
        bot = FakeBot(member=SimpleNamespace(status="restricted", is_member=False))
        assert await is_subscribed(bot, "@c", 1) is False

    asyncio.run(run())
