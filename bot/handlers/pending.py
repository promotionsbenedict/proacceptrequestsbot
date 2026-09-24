import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..database import Database
from ..helper import HelperUserbot, resolve_pending_count
from ..keyboards import (
    BTN_PENDING,
    cancel_keyboard,
    channel_manage_keyboard,
    pending_keyboard,
)
from ..messaging import deliver_message
from ..states import ApprovePending

logger = logging.getLogger(__name__)
router = Router()


async def _pending_entries(
    helper: HelperUserbot, db: Database, owner_id: int
) -> list[tuple]:
    entries: list[tuple] = []
    for channel in await db.list_channels(owner_id):
        count, _live = await resolve_pending_count(helper, db, channel)
        if count:
            entries.append((channel["id"], channel["title"] or "Channel", count))
    return entries


def _owns(channel, user_id: int) -> bool:
    return channel is not None and channel["owner_id"] == user_id


async def _deliver_welcomes(bot: Bot, db: Database, channel, user_ids: list[int]) -> None:
    """Best-effort welcome DM to freshly approved users we can identify."""
    for uid in user_ids:
        await db.remove_pending(channel["chat_id"], uid)
        await deliver_message(
            bot,
            db,
            uid,
            "there",
            channel["title"] or "the channel",
            "welcome",
        )


async def _approve_tracked(bot: Bot, db: Database, channel) -> tuple[int, int]:
    """Fallback: approve only locally-tracked requests via the Bot API."""
    approved = 0
    failed = 0
    for req in await db.list_pending(channel["chat_id"]):
        try:
            await bot.approve_chat_join_request(channel["chat_id"], req["user_id"])
            approved += 1
        except TelegramBadRequest as exc:
            logger.info("Approve failed for %s: %s", req["user_id"], exc)
            failed += 1
        await db.remove_pending(channel["chat_id"], req["user_id"])
        await deliver_message(
            bot,
            db,
            req["user_id"],
            req["first_name"] or "there",
            channel["title"] or "the channel",
            "welcome",
        )
    return approved, failed


@router.message(F.text == BTN_PENDING)
async def show_pending(message: Message, db: Database, helper: HelperUserbot) -> None:
    entries = await _pending_entries(helper, db, message.from_user.id)
    if not entries:
        await message.answer(
            "✅ No pending requests.\n\n"
            "Active channels approve new requests automatically. Connect a helper "
            "account to also clear requests that arrived before I became admin."
        )
        return
    await message.answer(
        "<b>⏳ Pending Requests</b>\n\nTap to approve everyone waiting for a channel.",
        reply_markup=pending_keyboard(entries),
    )


@router.callback_query(F.data.startswith("approve:"))
async def approve_all(
    query: CallbackQuery, db: Database, bot: Bot, helper: HelperUserbot
) -> None:
    channel_id = int(query.data.split(":", 1)[1])
    channel = await db.get_channel(channel_id)
    if not _owns(channel, query.from_user.id):
        await query.answer("Channel not found.", show_alert=True)
        return

    await query.answer("Working…")

    # Preferred path: helper userbot approves ALL requests (including historical).
    if helper.ready:
        access = await helper.ensure_access(bot, channel["chat_id"])
        if access.ok:
            result = await helper.approve_all(channel["chat_id"])
            # DM anyone we tracked locally, then clear our mirror.
            tracked = await db.list_pending(channel["chat_id"])
            await db.clear_pending(channel["chat_id"])
            for req in tracked:
                await deliver_message(
                    bot,
                    db,
                    req["user_id"],
                    req["first_name"] or "there",
                    channel["title"] or "the channel",
                    "welcome",
                )
            if result.approved:
                summary = (
                    f"✅ Approved <b>{result.approved}</b> pending request(s) for "
                    f"<b>{channel['title']}</b> — including any submitted before "
                    "I became an admin."
                )
            else:
                summary = (
                    f"ℹ️ {result.note or 'Nothing to approve right now.'}"
                )
            await _finish(query, db, helper, summary)
            return
        # Helper present but couldn't get access → tell the owner how to fix it,
        # and still clear whatever we can via the Bot API.
        approved, failed = await _approve_tracked(bot, db, channel)
        summary = f"⚠️ {access.message}"
        if approved:
            summary += f"\n\n✅ Meanwhile I approved {approved} tracked request(s)."
        await _finish(query, db, helper, summary)
        return

    # Fallback path: no helper configured.
    approved, failed = await _approve_tracked(bot, db, channel)
    if approved == 0 and failed == 0:
        summary = (
            f"No tracked pending requests for <b>{channel['title']}</b>.\n\n"
            "<i>Without a helper account, Telegram only lets me approve requests "
            "received while I am an admin. Configure the helper to clear older "
            "requests in bulk.</i>"
        )
    else:
        summary = (
            f"✅ Approved <b>{approved}</b> request(s) for <b>{channel['title']}</b>."
        )
        if failed:
            summary += (
                f"\n\n⚠️ {failed} could not be approved — they may have expired "
                "or were already handled."
            )
    await _finish(query, db, helper, summary)


async def _finish(
    query: CallbackQuery, db: Database, helper: HelperUserbot, summary: str
) -> None:
    entries = await _pending_entries(helper, db, query.from_user.id)
    await query.message.answer(summary, reply_markup=pending_keyboard(entries))


# ---------- custom amount ----------


@router.callback_query(F.data.startswith("custom:"))
async def custom_prompt(
    query: CallbackQuery, db: Database, state: FSMContext
) -> None:
    channel_id = int(query.data.split(":", 1)[1])
    channel = await db.get_channel(channel_id)
    if not _owns(channel, query.from_user.id):
        await query.answer("Channel not found.", show_alert=True)
        return
    await query.answer()
    await state.set_state(ApprovePending.waiting_for_amount)
    await state.update_data(channel_id=channel_id)
    await query.message.answer(
        f"How many pending requests should I approve for <b>{channel['title']}</b>?\n\n"
        "Send a number (oldest requests are approved first).",
        reply_markup=cancel_keyboard(),
    )


@router.message(ApprovePending.waiting_for_amount)
async def custom_receive(
    message: Message,
    db: Database,
    bot: Bot,
    helper: HelperUserbot,
    state: FSMContext,
) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Please send a positive whole number, e.g. <b>50</b>.")
        return
    amount = int(raw)

    data = await state.get_data()
    channel = await db.get_channel(int(data.get("channel_id", 0)))
    await state.clear()
    if not _owns(channel, message.from_user.id):
        await message.answer("Channel not found.")
        return

    if helper.ready:
        access = await helper.ensure_access(bot, channel["chat_id"])
        if access.ok:
            result = await helper.approve_amount(channel["chat_id"], amount)
            await _deliver_welcomes(bot, db, channel, result.user_ids)
            summary = (
                f"✅ Approved <b>{result.approved}</b> of {amount} requested for "
                f"<b>{channel['title']}</b>."
            )
            if result.failed:
                summary += f"\n\n⚠️ {result.failed} could not be approved."
            if result.note:
                summary += f"\n\n{result.note}"
            await message.answer(summary)
            return
        await message.answer(f"⚠️ {access.message}")
        return

    # Fallback: approve up to `amount` tracked requests via the Bot API.
    approved = 0
    failed = 0
    for req in (await db.list_pending(channel["chat_id"]))[:amount]:
        try:
            await bot.approve_chat_join_request(channel["chat_id"], req["user_id"])
            approved += 1
        except TelegramBadRequest as exc:
            logger.info("Approve failed for %s: %s", req["user_id"], exc)
            failed += 1
        await db.remove_pending(channel["chat_id"], req["user_id"])
        await deliver_message(
            bot,
            db,
            req["user_id"],
            req["first_name"] or "there",
            channel["title"] or "the channel",
            "welcome",
        )
    summary = (
        f"✅ Approved <b>{approved}</b> tracked request(s) for "
        f"<b>{channel['title']}</b>."
    )
    if failed:
        summary += f"\n\n⚠️ {failed} could not be approved."
    summary += (
        "\n\n<i>Configure the helper account to approve requests received "
        "before I became an admin.</i>"
    )
    await message.answer(summary)
