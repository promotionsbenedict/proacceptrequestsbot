import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ChatJoinRequest, ChatMemberUpdated

from ..database import Database
from ..messaging import deliver_message

logger = logging.getLogger(__name__)
router = Router()


@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, db: Database, bot: Bot) -> None:
    chat_id = request.chat.id
    user = request.from_user
    channel = await db.get_channel_by_chat(chat_id)

    if channel is None:
        # Not a chat we manage — leave it alone.
        return

    # Keep the channel title fresh.
    if request.chat.title and request.chat.title != channel["title"]:
        try:
            await db.db.execute(
                "UPDATE channels SET title = ? WHERE id = ?",
                (request.chat.title, channel["id"]),
            )
            await db.db.commit()
        except Exception:  # noqa: BLE001 - non-critical
            pass

    if not channel["is_active"]:
        # Queue for manual approval.
        await db.add_pending(chat_id, user.id, user.first_name, user.username)
        return

    try:
        await request.approve()
    except TelegramBadRequest as exc:
        logger.warning("Failed to approve %s in %s: %s", user.id, chat_id, exc)
        return

    if channel["welcome_enabled"]:
        await deliver_message(
            bot, db, user.id, user.first_name or "there", channel, "welcome"
        )


@router.chat_member()
async def on_chat_member(update: ChatMemberUpdated, db: Database, bot: Bot) -> None:
    """Send the goodbye message when a member leaves or is removed."""
    channel = await db.get_channel_by_chat(update.chat.id)
    if channel is None or not channel["goodbye_enabled"]:
        return

    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status
    was_member = old_status in {"member", "administrator", "creator", "restricted"}
    now_gone = new_status in {"left", "kicked"}

    if was_member and now_gone:
        user = update.new_chat_member.user
        if user.is_bot:
            return
        await deliver_message(
            bot, db, user.id, user.first_name or "there", channel, "goodbye"
        )
