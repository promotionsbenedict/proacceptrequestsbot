import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..config import Config
from ..database import Database
from ..keyboards import (
    BTN_ADMIN,
    admin_keyboard,
    cancel_keyboard,
    optout_keyboard,
    promoted_manage_keyboard,
)
from ..states import AdminBroadcast, AdminGrant, AdminPromote

logger = logging.getLogger(__name__)
router = Router()


def _is_admin(user_id: int, config: Config) -> bool:
    return user_id == config.admin_id


async def _render_admin_home(target: Message) -> None:
    await target.answer(
        "<b>🛠 Admin Panel</b>\n\nManage promoted channels, broadcasts, and premium access.",
        reply_markup=admin_keyboard(),
    )


@router.message(F.text == BTN_ADMIN)
async def admin_home_msg(message: Message, config: Config) -> None:
    if not _is_admin(message.from_user.id, config):
        return
    await _render_admin_home(message)


@router.callback_query(F.data == "admin_home")
async def admin_home_cb(query: CallbackQuery, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    await query.answer()
    await _render_admin_home(query.message)


# ---------- stats ----------


@router.callback_query(F.data == "admin_stats")
async def admin_stats(query: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    users = await db.count_users()
    channels = await db.count_all_channels()
    reachable = len(await db.list_broadcast_users())
    await query.answer()
    await query.message.answer(
        "<b>📊 Statistics</b>\n\n"
        f"Users: <b>{users}</b>\n"
        f"Connected channels: <b>{channels}</b>\n"
        f"Reachable (opted-in): <b>{reachable}</b>",
        reply_markup=admin_keyboard(),
    )


# ---------- promoted channels ----------


@router.callback_query(F.data == "admin_promoted")
async def admin_promoted(query: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    promoted = await db.list_promoted()
    lines = "\n".join(f"• {p['name']} — {p['url']}" for p in promoted) or "None yet."
    await query.answer()
    await query.message.answer(
        f"<b>📣 Promoted Channels</b>\n\n{lines}\n\n"
        "These buttons are appended to every welcome/goodbye message.",
        reply_markup=promoted_manage_keyboard(promoted),
    )


@router.callback_query(F.data == "add_promoted")
async def add_promoted(query: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    await state.set_state(AdminPromote.waiting_for_name)
    await query.answer()
    await query.message.answer(
        "Send the display name for the promoted button.", reply_markup=cancel_keyboard()
    )


@router.message(AdminPromote.waiting_for_name)
async def promoted_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(AdminPromote.waiting_for_url)
    await message.answer("Now send the URL (https:// or tg://).", reply_markup=cancel_keyboard())


@router.message(AdminPromote.waiting_for_url)
async def promoted_url(message: Message, db: Database, state: FSMContext) -> None:
    url = message.text.strip()
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
        await message.answer("❌ That doesn't look like a URL. Try again.")
        return
    data = await state.get_data()
    await db.add_promoted(data["name"], url)
    await state.clear()
    promoted = await db.list_promoted()
    await message.answer(
        "✅ Promoted channel added.", reply_markup=promoted_manage_keyboard(promoted)
    )


@router.callback_query(F.data.startswith("delpromo:"))
async def delete_promoted(query: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    promoted_id = int(query.data.split(":", 1)[1])
    await db.delete_promoted(promoted_id)
    await query.answer("Removed.")
    promoted = await db.list_promoted()
    try:
        await query.message.edit_reply_markup(
            reply_markup=promoted_manage_keyboard(promoted)
        )
    except TelegramBadRequest:
        pass


# ---------- broadcast ----------


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(query: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    await state.set_state(AdminBroadcast.waiting_for_content)
    await query.answer()
    await query.message.answer(
        "Send the message to broadcast to all opted-in users. "
        "Text, photos, and forwards all work.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminBroadcast.waiting_for_content)
async def do_broadcast(message: Message, db: Database, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    recipients = await db.list_broadcast_users()
    sent = 0
    failed = 0
    for chat_id in recipients:
        try:
            await message.send_copy(chat_id, reply_markup=optout_keyboard())
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            await db.opt_out_broadcast(chat_id)
            failed += 1
    await message.answer(
        f"📢 Broadcast complete.\nDelivered: <b>{sent}</b>\nFailed/blocked: <b>{failed}</b>",
        reply_markup=admin_keyboard(),
    )


# ---------- grant premium ----------


@router.callback_query(F.data == "admin_grant")
async def admin_grant(query: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    await state.set_state(AdminGrant.waiting_for_user)
    await query.answer()
    await query.message.answer(
        "Send the numeric user ID (or @username of a user who has started the bot) "
        "to grant Premium.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminGrant.waiting_for_user)
async def do_grant(message: Message, db: Database, state: FSMContext) -> None:
    raw = message.text.strip()
    user_id: int | None = None

    if raw.lstrip("-").isdigit():
        user_id = int(raw)
    else:
        username = raw.lstrip("@").lower()
        cur = await db.db.execute(
            "SELECT id FROM users WHERE LOWER(username) = ?", (username,)
        )
        row = await cur.fetchone()
        if row:
            user_id = row["id"]

    if user_id is None:
        await message.answer(
            "❌ Couldn't resolve that user. Send a numeric ID, or a @username of "
            "someone who has already started the bot."
        )
        return

    await db.upsert_user(user_id, None, None)
    await db.set_plan(user_id, "premium")
    await state.clear()
    await message.answer(
        f"⚡ Premium granted to <code>{user_id}</code>.", reply_markup=admin_keyboard()
    )
