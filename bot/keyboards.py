from typing import Optional

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# ---------- button labels used for reply-keyboard routing ----------

BTN_ADD = "➕ Add Channel"
BTN_MY = "📋 My Channels"
BTN_PENDING = "⏳ Pending"
BTN_PREMIUM = "⚡ Premium"
BTN_HELP = "❓ Help"
BTN_ADMIN = "🛠 Admin Panel"


def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_MY)],
        [KeyboardButton(text=BTN_PENDING), KeyboardButton(text=BTN_PREMIUM)],
        [KeyboardButton(text=BTN_HELP)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def subscription_keyboard(channel_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Join Channel", url=channel_url)],
            [InlineKeyboardButton(text="✅ I've Joined", callback_data="check_sub")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Cancel", callback_data="cancel")]
        ]
    )


def channels_list_keyboard(channels: list) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        status = "🟢" if ch["is_active"] else "🔴"
        title = ch["title"] or f"Chat {ch['chat_id']}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {title}", callback_data=f"ch:{ch['id']}"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Add Channel", callback_data="add_channel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_manage_keyboard(channel, pending_count: int = 0) -> InlineKeyboardMarkup:
    active = channel["is_active"]
    toggle_label = "⏸ Deactivate" if active else "▶️ Activate"
    approve_label = (
        f"✅ Approve All Pending ({pending_count})"
        if pending_count
        else "✅ Approve All Pending"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_label, callback_data=f"toggle:{channel['id']}")],
            [InlineKeyboardButton(text=approve_label, callback_data=f"approve:{channel['id']}")],
            [InlineKeyboardButton(text="🔢 Approve Custom Amount", callback_data=f"custom:{channel['id']}")],
            [InlineKeyboardButton(text="🗑 Remove", callback_data=f"del:{channel['id']}")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="my_channels")],
        ]
    )


def global_message_menu_keyboard(message, kind: str) -> InlineKeyboardMarkup:
    enabled = message["enabled"]
    toggle_label = "🔕 Disable" if enabled else "🔔 Enable"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_label, callback_data=f"gtoggle:{kind}")],
            [InlineKeyboardButton(text="📝 Set Text", callback_data=f"gsettext:{kind}")],
            [InlineKeyboardButton(text="🖼 Set Image", callback_data=f"gsetimg:{kind}")],
            [InlineKeyboardButton(text="🔘 Set Buttons", callback_data=f"gsetbtn:{kind}")],
            [InlineKeyboardButton(text="🧹 Clear Image", callback_data=f"gclrimg:{kind}")],
            [InlineKeyboardButton(text="🧹 Clear Buttons", callback_data=f"gclrbtn:{kind}")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="admin_home")],
        ]
    )


def confirm_delete_keyboard(channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Yes, remove", callback_data=f"delyes:{channel_id}"
                ),
                InlineKeyboardButton(text="✖️ No", callback_data=f"ch:{channel_id}"),
            ]
        ]
    )


def pending_keyboard(entries: list[tuple]) -> InlineKeyboardMarkup:
    """entries: list of (channel_id, title, pending_count)."""
    rows = []
    for channel_id, title, count in entries:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ Approve {count} · {title}",
                    callback_data=f"approve:{channel_id}",
                )
            ]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="⬅️ Back to Channels", callback_data="my_channels")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def approval_progress_keyboard(channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛑 Stop", callback_data=f"apstop:{channel_id}")]
        ]
    )


def premium_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Upgrade to Premium", callback_data="buy_premium")],
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Stats", callback_data="admin_stats")],
            [InlineKeyboardButton(text="👋 Welcome Message", callback_data="gmsg:welcome")],
            [InlineKeyboardButton(text="🚪 Goodbye Message", callback_data="gmsg:goodbye")],
            [InlineKeyboardButton(text="📣 Promoted Channels", callback_data="admin_promoted")],
            [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="⚡ Grant Premium", callback_data="admin_grant")],
        ]
    )


def promoted_manage_keyboard(promoted: list) -> InlineKeyboardMarkup:
    rows = []
    for p in promoted:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 {p['name']}", callback_data=f"delpromo:{p['id']}"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Add Promoted", callback_data="add_promoted")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def optout_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔕 Stop notifications", callback_data="opt_out")]
        ]
    )
