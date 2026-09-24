from aiogram import Dispatcher

from . import admin, channels, common, join_requests, messages, pending


def register_handlers(dp: Dispatcher) -> None:
    # Order matters: chat-member/join updates and specific routers first.
    dp.include_router(join_requests.router)
    dp.include_router(common.router)
    dp.include_router(channels.router)
    dp.include_router(messages.router)
    dp.include_router(pending.router)
    dp.include_router(admin.router)
