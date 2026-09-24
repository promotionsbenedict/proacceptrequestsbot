"""Regression tests for bug #1: goodbye messages not sending.

The old handler crashed for two reasons:
  * it read ``channel["goodbye_enabled"]`` — a column that does not exist in
    the ``channels`` table, raising immediately; and
  * it passed the whole ``channel`` Row as the ``chat_title`` argument instead
    of ``channel["title"]``, so ``str.replace`` blew up.

These tests exercise the real handler against a real in-memory database Row so
either regression would fail loudly.
"""

import asyncio
from types import SimpleNamespace

from bot.database import Database
from bot.handlers.join_requests import on_chat_member


class FakeBot:
    def __init__(self):
        self.sent_messages = []
        self.sent_photos = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent_messages.append((chat_id, text))

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        self.sent_photos.append((chat_id, photo, caption))


def _leave_update(chat_id, user_id, first_name):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        old_chat_member=SimpleNamespace(status="member"),
        new_chat_member=SimpleNamespace(
            status="left",
            user=SimpleNamespace(id=user_id, first_name=first_name, is_bot=False),
        ),
    )


async def _setup_db():
    db = Database(":memory:")
    await db.connect()
    await db.add_channel(owner_id=1, chat_id=-100, title="My Channel", chat_type="channel")
    return db


def test_goodbye_delivered_with_rendered_title():
    async def run():
        db = await _setup_db()
        await db.set_global_message_field("goodbye", "enabled", 1)
        await db.set_global_message_field(
            "goodbye", "text", "Bye {first_name} from {chat_title}"
        )
        bot = FakeBot()

        await on_chat_member(_leave_update(-100, 42, "Alice"), db, bot)

        assert bot.sent_messages == [(42, "Bye Alice from My Channel")]
        await db.close()

    asyncio.run(run())


def test_goodbye_skipped_when_disabled():
    async def run():
        db = await _setup_db()
        await db.set_global_message_field("goodbye", "enabled", 0)
        bot = FakeBot()

        await on_chat_member(_leave_update(-100, 42, "Alice"), db, bot)

        assert bot.sent_messages == []
        assert bot.sent_photos == []
        await db.close()

    asyncio.run(run())


def test_goodbye_ignores_unmanaged_chat():
    async def run():
        db = await _setup_db()
        await db.set_global_message_field("goodbye", "enabled", 1)
        bot = FakeBot()

        # chat_id that is not connected -> no goodbye, no crash.
        await on_chat_member(_leave_update(-999, 42, "Alice"), db, bot)

        assert bot.sent_messages == []
        await db.close()

    asyncio.run(run())


def test_goodbye_only_on_actual_departure():
    async def run():
        db = await _setup_db()
        await db.set_global_message_field("goodbye", "enabled", 1)
        await db.set_global_message_field("goodbye", "text", "Bye {first_name}")
        bot = FakeBot()

        # member -> member (e.g. promotion), not a departure.
        update = SimpleNamespace(
            chat=SimpleNamespace(id=-100),
            old_chat_member=SimpleNamespace(status="member"),
            new_chat_member=SimpleNamespace(
                status="administrator",
                user=SimpleNamespace(id=7, first_name="Bob", is_bot=False),
            ),
        )
        await on_chat_member(update, db, bot)

        assert bot.sent_messages == []
        await db.close()

    asyncio.run(run())
