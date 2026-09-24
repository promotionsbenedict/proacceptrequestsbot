import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .database import Database, decode_buttons

logger = logging.getLogger(__name__)


def render_text(template: Optional[str], *, first_name: str, chat_title: str) -> str:
    if not template:
        return ""
    return (
        template.replace("{first_name}", first_name)
        .replace("{chat_title}", chat_title)
        .replace("{name}", first_name)
    )


def parse_buttons(raw: str) -> list[dict]:
    """Parse one button per line in the form 'Label - https://url' or 'Label | https://url'."""
    buttons: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        sep = " - " if " - " in line else ("|" if "|" in line else None)
        if sep is None:
            continue
        label, _, url = line.partition(sep)
        label, url = label.strip(), url.strip()
        if label and (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
            buttons.append({"text": label, "url": url})
    return buttons


def build_keyboard(
    owner_buttons: list[dict], promoted: list
) -> Optional[InlineKeyboardMarkup]:
    rows = [[InlineKeyboardButton(text=b["text"], url=b["url"])] for b in owner_buttons]
    for p in promoted:
        rows.append([InlineKeyboardButton(text=f"📢 {p['name']}", url=p["url"])])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def deliver_message(
    bot: Bot,
    db: Database,
    user_id: int,
    first_name: str,
    chat_title: str,
    kind: str,
) -> bool:
    """Send the global welcome/goodbye DM to a user. Returns True on success.

    Message content is admin-controlled and global (see the ``global_messages``
    table). Delivery only works if the user has already started the bot
    (Telegram rule), so failures are expected and swallowed.
    """
    message = await db.get_global_message(kind)
    if message is None or not message["enabled"]:
        return False

    text = render_text(
        message["text"],
        first_name=first_name,
        chat_title=chat_title or "the channel",
    )
    image = message["image"]
    admin_buttons = decode_buttons(message["buttons"])
    promoted = await db.list_promoted()
    keyboard = build_keyboard(admin_buttons, promoted)

    try:
        if image:
            await bot.send_photo(
                user_id, photo=image, caption=text or None, reply_markup=keyboard
            )
        elif text:
            await bot.send_message(user_id, text, reply_markup=keyboard)
        else:
            return False
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logger.info("Could not DM user %s: %s", user_id, exc)
        return False
