from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..database import Database, decode_buttons, encode_buttons
from ..keyboards import cancel_keyboard, message_menu_keyboard
from ..messaging import parse_buttons
from ..states import CustomizeMessage

router = Router()

_KIND_LABEL = {"welcome": "👋 Welcome", "goodbye": "🚪 Goodbye"}


async def _owned_channel(query: CallbackQuery, db: Database, channel_id: int):
    channel = await db.get_channel(channel_id)
    if channel is None or channel["owner_id"] != query.from_user.id:
        await query.answer("Channel not found.", show_alert=True)
        return None
    return channel


def _describe(channel, kind: str) -> str:
    label = _KIND_LABEL[kind]
    enabled = "on" if channel[f"{kind}_enabled"] else "off"
    text = channel[f"{kind}_text"] or "—"
    has_image = "yes" if channel[f"{kind}_image"] else "no"
    buttons = decode_buttons(channel[f"{kind}_buttons"])
    return (
        f"<b>{label} message</b> for <b>{channel['title']}</b>\n\n"
        f"Status: {enabled}\n"
        f"Image: {has_image}\n"
        f"Buttons: {len(buttons)}\n\n"
        f"<b>Text:</b>\n{text}\n\n"
        "Placeholders: <code>{first_name}</code>, <code>{chat_title}</code>"
    )


@router.callback_query(F.data.startswith("msg:"))
async def open_message_menu(query: CallbackQuery, db: Database) -> None:
    _, kind, cid = query.data.split(":")
    channel = await _owned_channel(query, db, int(cid))
    if channel is None:
        return
    await query.answer()
    await query.message.answer(
        _describe(channel, kind), reply_markup=message_menu_keyboard(channel, kind)
    )


@router.callback_query(F.data.startswith("mtoggle:"))
async def toggle_message(query: CallbackQuery, db: Database) -> None:
    _, kind, cid = query.data.split(":")
    channel = await _owned_channel(query, db, int(cid))
    if channel is None:
        return
    new_value = 0 if channel[f"{kind}_enabled"] else 1
    await db.update_channel_field(int(cid), f"{kind}_enabled", new_value)
    await query.answer("Enabled 🔔" if new_value else "Disabled 🔕")
    updated = await db.get_channel(int(cid))
    try:
        await query.message.edit_reply_markup(
            reply_markup=message_menu_keyboard(updated, kind)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("settext:"))
async def ask_text(query: CallbackQuery, state: FSMContext) -> None:
    _, kind, cid = query.data.split(":")
    await state.set_state(CustomizeMessage.waiting_for_text)
    await state.update_data(channel_id=int(cid), kind=kind)
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
    await db.update_channel_field(data["channel_id"], f"{data['kind']}_text", message.text or "")
    await state.clear()
    channel = await db.get_channel(data["channel_id"])
    await message.answer(
        "✅ Text updated.", reply_markup=message_menu_keyboard(channel, data["kind"])
    )


@router.callback_query(F.data.startswith("setimg:"))
async def ask_image(query: CallbackQuery, state: FSMContext) -> None:
    _, kind, cid = query.data.split(":")
    await state.set_state(CustomizeMessage.waiting_for_image)
    await state.update_data(channel_id=int(cid), kind=kind)
    await query.answer()
    await query.message.answer(
        "Send a photo to attach to this message.", reply_markup=cancel_keyboard()
    )


@router.message(CustomizeMessage.waiting_for_image, F.photo)
async def save_image(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    file_id = message.photo[-1].file_id
    await db.update_channel_field(data["channel_id"], f"{data['kind']}_image", file_id)
    await state.clear()
    channel = await db.get_channel(data["channel_id"])
    await message.answer(
        "✅ Image saved.", reply_markup=message_menu_keyboard(channel, data["kind"])
    )


@router.message(CustomizeMessage.waiting_for_image)
async def image_expected(message: Message) -> None:
    await message.answer("Please send a photo, or tap Cancel.")


@router.callback_query(F.data.startswith("setbtn:"))
async def ask_buttons(query: CallbackQuery, state: FSMContext) -> None:
    _, kind, cid = query.data.split(":")
    await state.set_state(CustomizeMessage.waiting_for_buttons)
    await state.update_data(channel_id=int(cid), kind=kind)
    await query.answer()
    await query.message.answer(
        "Send inline buttons, one per line, in the form:\n\n"
        "<code>Label - https://example.com</code>\n\n"
        "You can send several lines for several buttons.",
        reply_markup=cancel_keyboard(),
    )


@router.message(CustomizeMessage.waiting_for_buttons)
async def save_buttons(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    buttons = parse_buttons(message.text or "")
    if not buttons:
        await message.answer(
            "❌ No valid buttons found. Use <code>Label - https://url</code> per line."
        )
        return
    await db.update_channel_field(
        data["channel_id"], f"{data['kind']}_buttons", encode_buttons(buttons)
    )
    await state.clear()
    channel = await db.get_channel(data["channel_id"])
    await message.answer(
        f"✅ Saved {len(buttons)} button(s).",
        reply_markup=message_menu_keyboard(channel, data["kind"]),
    )


@router.callback_query(F.data.startswith("clrimg:"))
async def clear_image(query: CallbackQuery, db: Database) -> None:
    _, kind, cid = query.data.split(":")
    channel = await _owned_channel(query, db, int(cid))
    if channel is None:
        return
    await db.update_channel_field(int(cid), f"{kind}_image", None)
    await query.answer("Image cleared.")
    updated = await db.get_channel(int(cid))
    try:
        await query.message.edit_reply_markup(
            reply_markup=message_menu_keyboard(updated, kind)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("clrbtn:"))
async def clear_buttons(query: CallbackQuery, db: Database) -> None:
    _, kind, cid = query.data.split(":")
    channel = await _owned_channel(query, db, int(cid))
    if channel is None:
        return
    await db.update_channel_field(int(cid), f"{kind}_buttons", None)
    await query.answer("Buttons cleared.")
    updated = await db.get_channel(int(cid))
    try:
        await query.message.edit_reply_markup(
            reply_markup=message_menu_keyboard(updated, kind)
        )
    except TelegramBadRequest:
        pass
