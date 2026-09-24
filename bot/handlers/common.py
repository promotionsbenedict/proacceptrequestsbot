from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..config import Config
from ..database import Database
from ..keyboards import (
    BTN_HELP,
    BTN_PREMIUM,
    main_menu,
    optout_keyboard,
    premium_keyboard,
)

router = Router()

HELP_TEXT = (
    "<b>Pro Accept Requests — Help</b>\n\n"
    "This bot automatically approves join requests for your channels and groups, "
    "then greets new members with a custom message.\n\n"
    "<b>Setup</b>\n"
    "1. Add this bot as an <b>administrator</b> to your channel/group with the "
    "<i>Add Members</i> / <i>Invite via link</i> right.\n"
    "2. Tap <b>➕ Add Channel</b> and forward any message from that channel, or send its @username.\n"
    "3. Customize the welcome message, then <b>Activate</b> the channel.\n\n"
    "<b>How approvals work</b>\n"
    "• While a channel is <b>active</b>, new join requests are approved instantly.\n"
    "• While <b>inactive</b>, requests are queued under <b>⏳ Pending</b> until you approve them.\n\n"
    "<b>Commands</b>\n"
    "/start — open the main menu\n"
    "/help — show this help"
)


def build_welcome(first_name: str) -> str:
    return (
        f"👋 Welcome, <b>{first_name}</b>!\n\n"
        "I'm <b>Pro Accept Requests</b> — I auto-approve join requests for your "
        "channels and groups and welcome new members for you.\n\n"
        "Use the menu below to add and manage your channels."
    )


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, config: Config) -> None:
    await db.upsert_user(
        message.from_user.id, message.from_user.username, message.from_user.first_name
    )
    # People who start the bot become reachable for welcome/broadcast messages.
    await db.add_broadcast_user(
        message.from_user.id,
        message.from_user.first_name,
        message.from_user.username,
    )
    is_admin = message.from_user.id == config.admin_id
    await message.answer(
        build_welcome(message.from_user.first_name),
        reply_markup=main_menu(is_admin),
    )


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(query: CallbackQuery, db: Database, config: Config) -> None:
    # Reaching this handler means the middleware already confirmed membership.
    await db.upsert_user(
        query.from_user.id, query.from_user.username, query.from_user.first_name
    )
    is_admin = query.from_user.id == config.admin_id
    await query.answer("Thanks for joining! 🎉")
    await query.message.answer(
        build_welcome(query.from_user.first_name), reply_markup=main_menu(is_admin)
    )


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def show_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text == BTN_PREMIUM)
async def show_premium(message: Message, db: Database) -> None:
    premium = await db.is_premium(message.from_user.id)
    if premium:
        await message.answer("⚡ You already have <b>Premium</b>. Enjoy unlimited channels!")
        return
    await message.answer(
        "<b>⚡ Premium</b>\n\n"
        "Free plan: manage <b>1</b> channel.\n"
        "Premium plan: <b>unlimited</b> channels, priority approvals, and no branding.\n\n"
        "Payments are coming soon — tap below to register your interest.",
        reply_markup=premium_keyboard(),
    )


@router.callback_query(F.data == "buy_premium")
async def cb_buy_premium(query: CallbackQuery, db: Database) -> None:
    await db.record_payment(
        user_id=query.from_user.id,
        plan="premium",
        amount=None,
        currency=None,
        provider="interest",
        status="interest",
    )
    await query.answer()
    await query.message.answer(
        "🙌 Thanks! Your interest has been recorded. "
        "We'll notify you as soon as Premium checkout goes live."
    )


@router.callback_query(F.data == "opt_out")
async def cb_opt_out(query: CallbackQuery, db: Database) -> None:
    await db.opt_out_broadcast(query.from_user.id)
    await query.answer("You won't receive further notifications.", show_alert=True)


@router.callback_query(F.data == "cancel")
async def cb_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer("Cancelled.")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
