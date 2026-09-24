import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from ..database import Database
from ..keyboards import BTN_PENDING, pending_keyboard
from ..messaging import deliver_message

logger = logging.getLogger(__name__)
router = Router()


async def _pending_entries(db: Database, owner_id: int) -> list[tuple]:
    entries: list[tuple] = []
    for channel in await db.list_channels(owner_id):
        count = await db.count_pending(channel["chat_id"])
        if count:
            entries.append(
                (channel["id"], channel["title"] or "Channel", count)
            )
    return entries


@router.message(F.text == BTN_PENDING)
async def show_pending(message: Message, db: Database) -> None:
    entries = await _pending_entries(db, message.from_user.id)
    if not entries:
        await message.answer(
            "✅ No pending requests.\n\n"
            "Requests only queue up while a channel is <b>inactive</b>. "
            "Active channels are approved automatically."
        )
        return
    await message.answer(
        "<b>⏳ Pending Requests</b>\n\nTap to approve everyone waiting for a channel.",
        reply_markup=pending_keyboard(entries),
    )


@router.callback_query(F.data.startswith("approve:"))
async def approve_all(query: CallbackQuery, db: Database, bot: Bot) -> None:
    channel_id = int(query.data.split(":", 1)[1])
    channel = await db.get_channel(channel_id)
    if channel is None or channel["owner_id"] != query.from_user.id:
        await query.answer("Channel not found.", show_alert=True)
        return

    pending = await db.list_pending(channel["chat_id"])
    approved = 0
    for req in pending:
        try:
            await bot.approve_chat_join_request(channel["chat_id"], req["user_id"])
            approved += 1
        except TelegramBadRequest as exc:
            logger.info("Approve failed for %s: %s", req["user_id"], exc)
        await db.remove_pending(channel["chat_id"], req["user_id"])
        if channel["welcome_enabled"]:
            await deliver_message(
                bot, db, req["user_id"], req["first_name"] or "there", channel, "welcome"
            )

    await query.answer(f"Approved {approved} request(s).")
    entries = await _pending_entries(db, query.from_user.id)
    await query.message.answer(
        f"✅ Approved <b>{approved}</b> request(s) for <b>{channel['title']}</b>.",
        reply_markup=pending_keyboard(entries),
    )
