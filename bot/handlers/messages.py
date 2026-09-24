import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..config import Config
from ..database import Database, decode_buttons, encode_buttons
from ..keyboards import cancel_keyboard, global_message_menu_keyboard
from ..messaging import parse_buttons
from ..states import CustomizeMessage

logger = logging.getLogger(__name__)
router = Router()

_KIND_LABEL = {"welcome": "👋 Welcome", "goodbye": "🚪 Goodbye"}


def _is_admin(user_id: int, config: Config) -> bool:
    return user_id == config.admin_id


def _describe(message, kind: str) -> str:
    label = _KIND_LABEL[kind]
    enabled = "on" if message["enabled"] else "off"
    text = message["text"] or "—"
    has_image = "yes" if message["image"] else "no"
    buttons = decode_buttons(message["buttons"])
    return (
        f"<b>{label} message</b> (global)\n\n"
        f"Status: {enabled}\n"
        f"Image: {has_image}\n"
        f"Buttons: {len(buttons)}\n\n"
        f"<b>Text:</b>\n{text}\n\n"
        "Placeholders: <code>{first_name}</code>, <code>{chat_title}</code>\n"
        "This message is used for every connected channel and group."
    )


@router.callback_query(F.data.startswith("gmsg:"))
async def open_message_menu(query: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    kind = query.data.split(":", 1)[1]
    message = await db.get_global_message(kind)
    await query.answer()
    await query.message.answer(
        _describe(message, kind), reply_markup=global_message_menu_keyboard(message, kind)
    )


@router.callback_query(F.data.startswith("gtoggle:"))
async def toggle_message(query: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    kind = query.data.split(":", 1)[1]
    message = await db.get_global_message(kind)
    new_value = 0 if message["enabled"] else 1
    await db.set_global_message_field(kind, "enabled", new_value)
    await query.answer("Enabled 🔔" if new_value else "Disabled 🔕")
    updated = await db.get_global_message(kind)
    try:
        await query.message.edit_reply_markup(
            reply_markup=global_message_menu_keyboard(updated, kind)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("gsettext:"))
async def ask_text(query: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    kind = query.data.split(":", 1)[1]
    await state.set_state(CustomizeMessage.waiting_for_text)
    await state.update_data(kind=kind)
    await query.answer()
    await query.message.answer(
        "Send the new message text.\n\n"
        "You can use <code>{first_name}</code> and <code>{chat_title}</code> "
        "as placeholders.",
        reply_markup=cancel_keyboard(),
    )


@router.message(CustomizeMessage.waiting_for_text)
async def save_text(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    kind = data["kind"]
    await db.set_global_message_field(kind, "text", message.text or "")
    await state.clear()
    updated = await db.get_global_message(kind)
    await message.answer(
        "✅ Text updated.", reply_markup=global_message_menu_keyboard(updated, kind)
    )


@router.callback_query(F.data.startswith("gsetimg:"))
async def ask_image(query: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    kind = query.data.split(":", 1)[1]
    await state.set_state(CustomizeMessage.waiting_for_image)
    await state.update_data(kind=kind)
    await query.answer()
    await query.message.answer(
        "Send a photo to attach to this message.", reply_markup=cancel_keyboard()
    )


@router.message(CustomizeMessage.waiting_for_image, F.photo)
async def save_image(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    kind = data["kind"]
    file_id = message.photo[-1].file_id
    await db.set_global_message_field(kind, "image", file_id)
    await state.clear()
    updated = await db.get_global_message(kind)
    await message.answer(
        "✅ Image saved.", reply_markup=global_message_menu_keyboard(updated, kind)
    )


@router.message(CustomizeMessage.waiting_for_image)
async def image_expected(message: Message) -> None:
    await message.answer("Please send a photo, or tap Cancel.")


@router.callback_query(F.data.startswith("gsetbtn:"))
async def ask_buttons(query: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    kind = query.data.split(":", 1)[1]
    await state.set_state(CustomizeMessage.waiting_for_buttons)
    await state.update_data(kind=kind)
    await query.answer()
    await query.message.answer(
        "Send promotional inline buttons, one per line, in the form:\n\n"
        "<code>Label - https://example.com</code>\n\n"
        "You can send several lines for several buttons.",
        reply_markup=cancel_keyboard(),
    )


@router.message(CustomizeMessage.waiting_for_buttons)
async def save_buttons(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    kind = data["kind"]
    buttons = parse_buttons(message.text or "")
    if not buttons:
        await message.answer(
            "❌ No valid buttons found. Use <code>Label - https://url</code> per line."
        )
        return
    await db.set_global_message_field(kind, "buttons", encode_buttons(buttons))
    await state.clear()
    updated = await db.get_global_message(kind)
    await message.answer(
        f"✅ Saved {len(buttons)} button(s).",
        reply_markup=global_message_menu_keyboard(updated, kind),
    )


@router.callback_query(F.data.startswith("gclrimg:"))
async def clear_image(query: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    kind = query.data.split(":", 1)[1]
    await db.set_global_message_field(kind, "image", None)
    await query.answer("Image cleared.")
    updated = await db.get_global_message(kind)
    try:
        await query.message.edit_reply_markup(
            reply_markup=global_message_menu_keyboard(updated, kind)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("gclrbtn:"))
async def clear_buttons(query: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(query.from_user.id, config):
        await query.answer()
        return
    kind = query.data.split(":", 1)[1]
    await db.set_global_message_field(kind, "buttons", None)
    await query.answer("Buttons cleared.")
    updated = await db.get_global_message(kind)
    try:
        await query.message.edit_reply_markup(
            reply_markup=global_message_menu_keyboard(updated, kind)
        )
    except TelegramBadRequest:
        pass
