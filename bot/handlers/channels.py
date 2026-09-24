import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Chat, Message

from ..config import Config
from ..database import FREE_CHANNEL_LIMIT, Database
from ..keyboards import (
    BTN_ADD,
    BTN_MY,
    cancel_keyboard,
    channel_manage_keyboard,
    channels_list_keyboard,
    confirm_delete_keyboard,
)
from ..states import AddChannel

logger = logging.getLogger(__name__)
router = Router()

_ADMIN_STATUSES = {"creator", "administrator"}


async def _render_channels(target: Message, db: Database, owner_id: int) -> None:
    channels = await db.list_channels(owner_id)
    if not channels:
        text = (
            "You have no channels yet.\n\n"
            "Tap <b>➕ Add Channel</b> to connect your first channel or group."
        )
    else:
        text = (
            "<b>📋 Your Channels</b>\n\n"
            "🟢 = active (auto-approving)   🔴 = inactive\n\n"
            "Tap a channel to manage it."
        )
    await target.answer(text, reply_markup=channels_list_keyboard(channels))


@router.message(F.text == BTN_MY)
async def my_channels_msg(message: Message, db: Database) -> None:
    await _render_channels(message, db, message.from_user.id)


@router.callback_query(F.data == "my_channels")
async def my_channels_cb(query: CallbackQuery, db: Database) -> None:
    await query.answer()
    await _render_channels(query.message, db, query.from_user.id)


# ---------- add channel flow ----------


async def _start_add(target: Message, db: Database, owner_id: int, state: FSMContext) -> None:
    if not await db.is_premium(owner_id):
        count = await db.count_channels(owner_id)
        if count >= FREE_CHANNEL_LIMIT:
            await target.answer(
                f"⚠️ Free plan is limited to <b>{FREE_CHANNEL_LIMIT}</b> channel(s).\n"
                "Upgrade to <b>⚡ Premium</b> for unlimited channels."
            )
            return
    await state.set_state(AddChannel.waiting_for_channel)
    await target.answer(
        "<b>➕ Add a Channel</b>\n\n"
        "1. Add me as an <b>administrator</b> to your channel/group with the "
        "<i>Add Members</i> right.\n"
        "2. Then either:\n"
        "   • forward any message from that channel here, or\n"
        "   • send its public @username.",
        reply_markup=cancel_keyboard(),
    )


@router.message(F.text == BTN_ADD)
async def add_channel_msg(message: Message, db: Database, state: FSMContext) -> None:
    await _start_add(message, db, message.from_user.id, state)


@router.callback_query(F.data == "add_channel")
async def add_channel_cb(query: CallbackQuery, db: Database, state: FSMContext) -> None:
    await query.answer()
    await _start_add(query.message, db, query.from_user.id, state)


def _extract_forward_chat(message: Message) -> Chat | None:
    origin = getattr(message, "forward_origin", None)
    if origin is not None and getattr(origin, "chat", None) is not None:
        return origin.chat
    # Fallback for older payloads.
    return getattr(message, "forward_from_chat", None)


@router.message(AddChannel.waiting_for_channel)
async def receive_channel(
    message: Message, db: Database, state: FSMContext, bot: Bot
) -> None:
    chat: Chat | None = _extract_forward_chat(message)

    if chat is None and message.text:
        handle = message.text.strip()
        if handle.startswith("https://t.me/"):
            handle = "@" + handle.rsplit("/", 1)[-1]
        if not handle.startswith("@"):
            handle = "@" + handle
        try:
            chat = await bot.get_chat(handle)
        except TelegramBadRequest:
            await message.answer(
                "❌ I couldn't find that chat. Make sure the @username is correct "
                "and I've been added as an administrator, then try again."
            )
            return

    if chat is None or chat.type not in {"channel", "supergroup", "group"}:
        await message.answer(
            "❌ That's not a channel or group. Forward a message from your channel "
            "or send its @username."
        )
        return

    # Verify the bot is an admin able to approve requests.
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramBadRequest:
        await message.answer(
            "❌ I'm not a member of that chat yet. Add me as an administrator first."
        )
        return
    if me.status != "administrator":
        await message.answer(
            "❌ I need to be an <b>administrator</b> (with the <i>Add Members</i> "
            "right) in that chat. Please update my permissions and try again."
        )
        return

    # Verify the requester actually manages the chat.
    try:
        requester = await bot.get_chat_member(chat.id, message.from_user.id)
    except TelegramBadRequest:
        requester = None
    if requester is None or requester.status not in _ADMIN_STATUSES:
        await message.answer(
            "❌ Only an administrator of that chat can connect it. "
            "Ask the owner to add it, or get admin rights first."
        )
        return

    channel_id = await db.add_channel(
        owner_id=message.from_user.id,
        chat_id=chat.id,
        title=chat.title,
        chat_type=chat.type,
    )
    await db.set_channel_active(channel_id, True)
    await state.clear()

    channel = await db.get_channel(channel_id)
    pending = await db.count_pending(chat.id)
    await message.answer(
        f"✅ Connected <b>{chat.title}</b> and activated auto-approval.\n\n"
        "New join requests will now be approved automatically. Use "
        "<b>✅ Approve All Pending</b> to clear requests that are already waiting.\n\n"
        "<i>Note: Telegram does not let bots fetch join requests that were "
        "submitted before I became an administrator. Only requests I receive "
        "while I am an admin can be approved automatically or in bulk.</i>",
        reply_markup=channel_manage_keyboard(channel, pending),
    )


# ---------- manage a single channel ----------


async def _show_manage(query: CallbackQuery, db: Database, channel_id: int) -> None:
    channel = await db.get_channel(channel_id)
    if channel is None or channel["owner_id"] != query.from_user.id:
        await query.answer("Channel not found.", show_alert=True)
        return
    pending = await db.count_pending(channel["chat_id"])
    status = "🟢 Active" if channel["is_active"] else "🔴 Inactive"
    text = (
        f"<b>{channel['title'] or 'Channel'}</b>\n\n"
        f"Status: {status}\n"
        f"Pending requests: {pending}"
    )
    await query.message.answer(
        text, reply_markup=channel_manage_keyboard(channel, pending)
    )


@router.callback_query(F.data.startswith("ch:"))
async def open_channel(query: CallbackQuery, db: Database) -> None:
    await query.answer()
    channel_id = int(query.data.split(":", 1)[1])
    await _show_manage(query, db, channel_id)


@router.callback_query(F.data.startswith("toggle:"))
async def toggle_channel(query: CallbackQuery, db: Database) -> None:
    channel_id = int(query.data.split(":", 1)[1])
    channel = await db.get_channel(channel_id)
    if channel is None or channel["owner_id"] != query.from_user.id:
        await query.answer("Channel not found.", show_alert=True)
        return
    new_active = not channel["is_active"]
    await db.set_channel_active(channel_id, new_active)
    await query.answer("Activated ✅" if new_active else "Deactivated ⏸")
    updated = await db.get_channel(channel_id)
    pending = await db.count_pending(updated["chat_id"])
    try:
        await query.message.edit_reply_markup(
            reply_markup=channel_manage_keyboard(updated, pending)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("del:"))
async def delete_prompt(query: CallbackQuery, db: Database) -> None:
    channel_id = int(query.data.split(":", 1)[1])
    channel = await db.get_channel(channel_id)
    if channel is None or channel["owner_id"] != query.from_user.id:
        await query.answer("Channel not found.", show_alert=True)
        return
    await query.answer()
    await query.message.answer(
        f"Remove <b>{channel['title']}</b>? Pending requests for it will be discarded.",
        reply_markup=confirm_delete_keyboard(channel_id),
    )


@router.callback_query(F.data.startswith("delyes:"))
async def delete_confirmed(query: CallbackQuery, db: Database) -> None:
    channel_id = int(query.data.split(":", 1)[1])
    channel = await db.get_channel(channel_id)
    if channel is None or channel["owner_id"] != query.from_user.id:
        await query.answer("Channel not found.", show_alert=True)
        return
    await db.delete_channel(channel_id)
    await query.answer("Removed.")
    await _render_channels(query.message, db, query.from_user.id)
