from aiogram.fsm.state import State, StatesGroup


class AddChannel(StatesGroup):
    waiting_for_channel = State()


class ApprovePending(StatesGroup):
    """Ask the owner how many pending requests to approve.

    `state.data` carries {"channel_id": int}.
    """

    waiting_for_amount = State()


class CustomizeMessage(StatesGroup):
    """Shared states for editing the global welcome/goodbye content (admin only).

    `state.data` carries {"kind": "welcome" | "goodbye"}.
    """

    waiting_for_text = State()
    waiting_for_image = State()
    waiting_for_buttons = State()


class AdminPromote(StatesGroup):
    waiting_for_name = State()
    waiting_for_url = State()


class AdminBroadcast(StatesGroup):
    waiting_for_content = State()


class AdminGrant(StatesGroup):
    waiting_for_user = State()
