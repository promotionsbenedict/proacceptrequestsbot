import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, TelegramObject

from .config import Config
from .keyboards import subscription_keyboard

logger = logging.getLogger(__name__)

_MEMBER_STATUSES = {"creator", "administrator", "member"}


async def is_subscribed(bot: Bot, channel: str, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(channel, user_id)
    except TelegramBadRequest as exc:
        # If the bot can't read the channel, don't lock users out.
        logger.warning("Membership check failed for %s: %s", channel, exc)
        return True
    if member.status in _MEMBER_STATUSES:
        return True
    # 'restricted' members are still in the chat when is_member is True.
    return getattr(member, "is_member", False)


class SubscriptionMiddleware(BaseMiddleware):
    """Force every user to join the mandatory channel before using the bot."""

    def __init__(self, config: Config) -> None:
        self.config = config

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        bot: Bot = data["bot"]

        if user is None or user.id == self.config.admin_id or user.is_bot:
            return await handler(event, data)

        if await is_subscribed(bot, self.config.required_channel, user.id):
            return await handler(event, data)

        prompt = (
            "🔒 To use this bot you must first join our channel.\n\n"
            "Join, then tap ✅ I've Joined."
        )
        keyboard = subscription_keyboard(self.config.required_channel_url)

        if isinstance(event, CallbackQuery):
            await event.answer("Please join the channel first.", show_alert=True)
            if event.message:
                try:
                    await event.message.answer(prompt, reply_markup=keyboard)
                except TelegramBadRequest:
                    pass
        elif isinstance(event, Message):
            await event.answer(prompt, reply_markup=keyboard)
        # Block the update from reaching handlers.
        return None
