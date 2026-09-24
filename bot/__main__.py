import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .config import load_config
from .database import Database
from .handlers import register_handlers
from .middlewares import SubscriptionMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Open the main menu"),
            BotCommand(command="help", description="How to use the bot"),
        ]
    )


async def main() -> None:
    config = load_config()

    db = Database(config.database_path)
    await db.connect()
    logger.info("Database ready at %s", config.database_path)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Make shared services injectable into handlers.
    dp["db"] = db
    dp["config"] = config

    # Enforce mandatory-channel membership on all user interactions.
    sub_mw = SubscriptionMiddleware(config)
    dp.message.middleware(sub_mw)
    dp.callback_query.middleware(sub_mw)

    register_handlers(dp)

    await set_commands(bot)

    allowed = dp.resolve_used_update_types()
    logger.info("Starting polling. Allowed updates: %s", allowed)
    try:
        await dp.start_polling(bot, allowed_updates=allowed)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
