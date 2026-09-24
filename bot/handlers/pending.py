import asyncio
import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..database import Database
from ..helper import ApproveResult, HelperUserbot, resolve_pending_count
from ..jobs import ApprovalJob, JobManager
from ..keyboards import (
    BTN_PENDING,
    approval_progress_keyboard,
    cancel_keyboard,
    pending_keyboard,
)
from ..messaging import deliver_message
from ..states import ApprovePending

logger = logging.getLogger(__name__)
router = Router()

# Minimum seconds between progress-message edits (Telegram rate-limit friendly).
PROGRESS_INTERVAL = 2.5


async def _pending_entries(
    helper: HelperUserbot, db: Database, owner_id: int
) -> list[tuple]:
    entries: list[tuple] = []
    for channel in await db.list_channels(owner_id):
        count, _live = await resolve_pending_count(helper, db, channel)
        if count:
            entries.append((channel["id"], channel["title"] or "Channel", count))
    return entries


def _owns(channel, user_id: int) -> bool:
    return channel is not None and channel["owner_id"] == user_id


def _progress_text(job: ApprovalJob) -> str:
    total = job.target if job.mode == "amount" else job.total
    total_str = str(total) if total else "…"
    return (
        f"⏳ Approving pending requests for <b>{job.title}</b>…\n\n"
        f"Approved <b>{job.approved}</b> / {total_str}\n\n"
        "<i>This runs in the background — you can keep using the bot. "
        "Tap Stop to cancel.</i>"
    )


async def _safe_edit(bot: Bot, chat_id: int, message_id: int, text: str, kb=None) -> None:
    try:
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, reply_markup=kb
        )
    except TelegramBadRequest:
        # Usually "message is not modified" — safe to ignore.
        pass


async def _deliver_welcomes(bot: Bot, db: Database, channel, user_ids: list[int]) -> None:
    """Best-effort welcome DM to freshly approved users we can identify."""
    for uid in user_ids:
        await db.remove_pending(channel["chat_id"], uid)
        await deliver_message(
            bot,
            db,
            uid,
            "there",
            channel["title"] or "the channel",
            "welcome",
        )


async def _deliver_tracked(bot: Bot, db: Database, channel) -> None:
    """DM everyone in our local mirror, then clear it."""
    tracked = await db.list_pending(channel["chat_id"])
    await db.clear_pending(channel["chat_id"])
    for req in tracked:
        await deliver_message(
            bot,
            db,
            req["user_id"],
            req["first_name"] or "there",
            channel["title"] or "the channel",
            "welcome",
        )


async def _approve_tracked(bot: Bot, db: Database, channel) -> tuple[int, int]:
    """Fallback: approve only locally-tracked requests via the Bot API."""
    approved = 0
    failed = 0
    for req in await db.list_pending(channel["chat_id"]):
        try:
            await bot.approve_chat_join_request(channel["chat_id"], req["user_id"])
            approved += 1
        except TelegramBadRequest as exc:
            logger.info("Approve failed for %s: %s", req["user_id"], exc)
            failed += 1
        await db.remove_pending(channel["chat_id"], req["user_id"])
        await deliver_message(
            bot,
            db,
            req["user_id"],
            req["first_name"] or "there",
            channel["title"] or "the channel",
            "welcome",
        )
    return approved, failed


@router.message(F.text == BTN_PENDING)
async def show_pending(message: Message, db: Database, helper: HelperUserbot) -> None:
    entries = await _pending_entries(helper, db, message.from_user.id)
    if not entries:
        await message.answer(
            "✅ No pending requests.\n\n"
            "Active channels approve new requests automatically. Connect a helper "
            "account to also clear requests that arrived before I became admin."
        )
        return
    await message.answer(
        "<b>⏳ Pending Requests</b>\n\nTap to approve everyone waiting for a channel.",
        reply_markup=pending_keyboard(entries),
    )


# ---------- Approve All (continuous background job) ----------


@router.callback_query(F.data.startswith("approve:"))
async def approve_all(
    query: CallbackQuery,
    db: Database,
    bot: Bot,
    helper: HelperUserbot,
    jobs: JobManager,
) -> None:
    channel_id = int(query.data.split(":", 1)[1])
    channel = await db.get_channel(channel_id)
    if not _owns(channel, query.from_user.id):
        await query.answer("Channel not found.", show_alert=True)
        return

    # Preferred path: helper userbot approves ALL requests (incl. historical).
    if helper.ready:
        if jobs.running(channel["chat_id"]):
            await query.answer(
                "Already approving this channel. Tap Stop on the progress "
                "message to cancel.",
                show_alert=True,
            )
            return

        await query.answer("Starting…")
        access = await helper.ensure_access(bot, channel["chat_id"])
        if not access.ok:
            approved, _failed = await _approve_tracked(bot, db, channel)
            summary = f"⚠️ {access.message}"
            if approved:
                summary += f"\n\n✅ Meanwhile I approved {approved} tracked request(s)."
            await _finish(query, db, helper, summary)
            return

        job = ApprovalJob(
            chat_id=channel["chat_id"],
            channel_id=channel_id,
            title=channel["title"] or "the channel",
            mode="all",
        )
        jobs.register(job)
        status = await query.message.answer(
            _progress_text(job), reply_markup=approval_progress_keyboard(channel_id)
        )
        job.task = asyncio.create_task(
            _run_all_job(bot, db, helper, jobs, job, status.chat.id, status.message_id)
        )
        return

    # Fallback path: no helper configured.
    await query.answer("Working…")
    approved, failed = await _approve_tracked(bot, db, channel)
    if approved == 0 and failed == 0:
        summary = (
            f"No tracked pending requests for <b>{channel['title']}</b>.\n\n"
            "<i>Without a helper account, Telegram only lets me approve requests "
            "received while I am an admin. Configure the helper to clear older "
            "requests in bulk.</i>"
        )
    else:
        summary = (
            f"✅ Approved <b>{approved}</b> request(s) for <b>{channel['title']}</b>."
        )
        if failed:
            summary += (
                f"\n\n⚠️ {failed} could not be approved — they may have expired "
                "or were already handled."
            )
    await _finish(query, db, helper, summary)


async def _run_all_job(
    bot: Bot,
    db: Database,
    helper: HelperUserbot,
    jobs: JobManager,
    job: ApprovalJob,
    owner_chat_id: int,
    status_message_id: int,
) -> None:
    last_edit = 0.0

    async def progress(done: int, total: int) -> None:
        nonlocal last_edit
        job.approved = done
        job.total = max(job.total, total, done)
        now = time.monotonic()
        if now - last_edit < PROGRESS_INTERVAL:
            return
        last_edit = now
        await _safe_edit(
            bot,
            owner_chat_id,
            status_message_id,
            _progress_text(job),
            approval_progress_keyboard(job.channel_id),
        )

    try:
        result = await helper.approve_all_stream(
            job.chat_id, progress=progress, cancelled=lambda: job.cancelled
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("approve-all job crashed for %s: %s", job.chat_id, exc)
        result = ApproveResult(
            approved=job.approved, note="Unexpected error; partial progress saved."
        )

    job.approved = result.approved
    channel = await db.get_channel(job.channel_id)

    # On a clean, non-cancelled run, DM tracked users and clear the mirror.
    if channel and not result.cancelled and result.approved:
        await _deliver_tracked(bot, db, channel)

    if result.cancelled:
        summary = (
            f"🛑 Stopped. Approved <b>{result.approved}</b> request(s) for "
            f"<b>{job.title}</b> before cancelling.\n\n"
            "<i>Tap Approve All again to resume from what's still pending.</i>"
        )
    elif result.approved:
        summary = (
            f"✅ Approved <b>{result.approved}</b> pending request(s) for "
            f"<b>{job.title}</b> — including any submitted before I became an admin."
        )
        if result.note:
            summary += f"\n\n{result.note}"
    else:
        summary = f"ℹ️ {result.note or 'Nothing to approve right now.'}"

    jobs.finish(job.chat_id)
    await _safe_edit(bot, owner_chat_id, status_message_id, summary)
    entries = await _pending_entries(helper, db, owner_chat_id)
    if entries:
        await bot.send_message(
            owner_chat_id,
            "Anything else to approve?",
            reply_markup=pending_keyboard(entries),
        )


@router.callback_query(F.data.startswith("apstop:"))
async def stop_job(query: CallbackQuery, db: Database, jobs: JobManager) -> None:
    channel_id = int(query.data.split(":", 1)[1])
    channel = await db.get_channel(channel_id)
    if not _owns(channel, query.from_user.id):
        await query.answer("Channel not found.", show_alert=True)
        return
    job = jobs.running(channel["chat_id"])
    if not job:
        await query.answer("No active approval to stop.")
        return
    job.request_cancel()
    await query.answer("Stopping… finishing the current batch.")


async def _finish(
    query: CallbackQuery, db: Database, helper: HelperUserbot, summary: str
) -> None:
    entries = await _pending_entries(helper, db, query.from_user.id)
    await query.message.answer(summary, reply_markup=pending_keyboard(entries))


# ---------- Approve Custom Amount (independent background job) ----------


@router.callback_query(F.data.startswith("custom:"))
async def custom_prompt(
    query: CallbackQuery, db: Database, state: FSMContext
) -> None:
    channel_id = int(query.data.split(":", 1)[1])
    channel = await db.get_channel(channel_id)
    if not _owns(channel, query.from_user.id):
        await query.answer("Channel not found.", show_alert=True)
        return
    await query.answer()
    await state.set_state(ApprovePending.waiting_for_amount)
    await state.update_data(channel_id=channel_id)
    await query.message.answer(
        f"How many pending requests should I approve for <b>{channel['title']}</b>?\n\n"
        "Send a number (oldest requests are approved first).",
        reply_markup=cancel_keyboard(),
    )


@router.message(ApprovePending.waiting_for_amount)
async def custom_receive(
    message: Message,
    db: Database,
    bot: Bot,
    helper: HelperUserbot,
    jobs: JobManager,
    state: FSMContext,
) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Please send a positive whole number, e.g. <b>50</b>.")
        return
    amount = int(raw)

    data = await state.get_data()
    channel = await db.get_channel(int(data.get("channel_id", 0)))
    await state.clear()
    if not _owns(channel, message.from_user.id):
        await message.answer("Channel not found.")
        return

    if helper.ready:
        if jobs.running(channel["chat_id"]):
            await message.answer(
                "⚠️ I'm already approving requests for this channel. "
                "Let it finish (or Stop it) before starting another run."
            )
            return

        access = await helper.ensure_access(bot, channel["chat_id"])
        if not access.ok:
            await message.answer(f"⚠️ {access.message}")
            return

        job = ApprovalJob(
            chat_id=channel["chat_id"],
            channel_id=channel["id"],
            title=channel["title"] or "the channel",
            mode="amount",
            target=amount,
        )
        jobs.register(job)
        status = await message.answer(
            _progress_text(job),
            reply_markup=approval_progress_keyboard(channel["id"]),
        )
        job.task = asyncio.create_task(
            _run_amount_job(
                bot, db, helper, jobs, job, amount, status.chat.id, status.message_id
            )
        )
        return

    # Fallback: approve up to `amount` tracked requests via the Bot API.
    approved = 0
    failed = 0
    for req in (await db.list_pending(channel["chat_id"]))[:amount]:
        try:
            await bot.approve_chat_join_request(channel["chat_id"], req["user_id"])
            approved += 1
        except TelegramBadRequest as exc:
            logger.info("Approve failed for %s: %s", req["user_id"], exc)
            failed += 1
        await db.remove_pending(channel["chat_id"], req["user_id"])
        await deliver_message(
            bot,
            db,
            req["user_id"],
            req["first_name"] or "there",
            channel["title"] or "the channel",
            "welcome",
        )
    summary = (
        f"✅ Approved <b>{approved}</b> tracked request(s) for "
        f"<b>{channel['title']}</b>."
    )
    if failed:
        summary += f"\n\n⚠️ {failed} could not be approved."
    summary += (
        "\n\n<i>Configure the helper account to approve requests received "
        "before I became an admin.</i>"
    )
    await message.answer(summary)


async def _run_amount_job(
    bot: Bot,
    db: Database,
    helper: HelperUserbot,
    jobs: JobManager,
    job: ApprovalJob,
    amount: int,
    owner_chat_id: int,
    status_message_id: int,
) -> None:
    last_edit = 0.0

    async def progress(done: int, total: int) -> None:
        nonlocal last_edit
        job.approved = done
        now = time.monotonic()
        if now - last_edit < PROGRESS_INTERVAL:
            return
        last_edit = now
        await _safe_edit(
            bot,
            owner_chat_id,
            status_message_id,
            _progress_text(job),
            approval_progress_keyboard(job.channel_id),
        )

    try:
        result = await helper.approve_amount_stream(
            job.chat_id, amount, progress=progress, cancelled=lambda: job.cancelled
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("approve-amount job crashed for %s: %s", job.chat_id, exc)
        result = ApproveResult(note="Unexpected error while approving.")

    channel = await db.get_channel(job.channel_id)
    if channel and result.user_ids:
        await _deliver_welcomes(bot, db, channel, result.user_ids)

    if result.cancelled:
        summary = (
            f"🛑 Stopped. Approved <b>{result.approved}</b> of {amount} requested "
            f"for <b>{job.title}</b>."
        )
    else:
        summary = (
            f"✅ Approved <b>{result.approved}</b> of {amount} requested for "
            f"<b>{job.title}</b>."
        )
    if result.failed:
        summary += f"\n\n⚠️ {result.failed} could not be approved."
    if result.note:
        summary += f"\n\n{result.note}"

    jobs.finish(job.chat_id)
    await _safe_edit(bot, owner_chat_id, status_message_id, summary)
