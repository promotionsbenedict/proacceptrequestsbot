"""Telethon helper userbot.

A single, centrally-managed Telegram *user* account (configured through
environment variables) that the bot uses over MTProto to do the one thing the
Bot API cannot: read and approve join requests that were submitted *before* the
bot became an administrator.

The helper is entirely optional. When the credentials are absent or the session
is not authorized, ``ready`` stays ``False`` and every handler falls back to the
plain aiogram behaviour (approving only requests tracked locally).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Longest single FloodWait we will sit through automatically before stopping a
# run and asking the owner to resume later. Telegram hands out short waits for
# bulk approvals; anything longer than this almost always means a temporary ban.
MAX_FLOOD_WAIT = 3600  # seconds

# How many *consecutive* recoverable errors (timeouts, server/RPC failures,
# dropped connections) we ride out before pausing a run. Each retry waits with
# exponential backoff and re-counts the queue, so we never lose progress or
# double-count — we simply resume from whatever is still pending on Telegram.
MAX_RECOVERABLE_RETRIES = 12

# Upper bound (seconds) on the exponential backoff between recoverable retries.
MAX_BACKOFF = 60

# Optional callbacks used by the streaming approvers.
ProgressCb = Callable[[int, int], Awaitable[None]]
CancelCb = Callable[[], bool]

# Imported lazily so the project still runs (and imports cleanly) when Telethon
# is not installed or the helper is unused.
try:  # pragma: no cover - import guard
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession
    from telethon.tl.functions.messages import (
        GetChatInviteImportersRequest,
        HideAllChatJoinRequestsRequest,
        HideChatJoinRequestRequest,
        ImportChatInviteRequest,
    )
    from telethon.tl.types import InputUser, InputUserEmpty

    TELETHON_AVAILABLE = True
except Exception as exc:  # pragma: no cover - import guard
    logger.warning("Telethon unavailable, helper disabled: %s", exc)
    TELETHON_AVAILABLE = False


@dataclass
class AccessResult:
    """Outcome of trying to give the helper admin access to a chat."""

    ok: bool
    status: str  # "ok" | "manual" | "disabled" | "error"
    message: str
    helper_username: Optional[str] = None


@dataclass
class ApproveResult:
    """Outcome of a bulk/partial approval run."""

    approved: int = 0
    failed: int = 0
    user_ids: list[int] = field(default_factory=list)
    note: str = ""
    cancelled: bool = False


def _invite_hash(link: str) -> Optional[str]:
    """Extract the invite hash from a t.me link created by the bot."""
    if not link:
        return None
    link = link.strip()
    if "joinchat/" in link:
        return link.rsplit("joinchat/", 1)[-1]
    if "/+" in link:
        return link.rsplit("/+", 1)[-1]
    if link.startswith("+"):
        return link[1:]
    return None


class HelperUserbot:
    def __init__(
        self,
        api_id: Optional[int],
        api_hash: Optional[str],
        session: Optional[str],
    ) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session
        self._client = None
        self._me = None
        self._ready = False
        # Serialize individual MTProto calls; a single user session is not meant
        # for heavy concurrency. The lock is held only for the duration of one
        # request (never across sleeps), so long-running jobs on one chat don't
        # block quick operations on another.
        self._lock = asyncio.Lock()

    # ---------- lifecycle ----------

    @property
    def configured(self) -> bool:
        return bool(
            TELETHON_AVAILABLE
            and self._api_id
            and self._api_hash
            and self._session
        )

    @property
    def ready(self) -> bool:
        return self._ready and self._client is not None

    @property
    def username(self) -> Optional[str]:
        return getattr(self._me, "username", None) if self._me else None

    async def start(self) -> None:
        if not self.configured:
            return
        try:
            self._client = TelegramClient(
                StringSession(self._session), int(self._api_id), self._api_hash
            )
            await self._client.connect()
            if not await self._client.is_user_authorized():
                logger.warning(
                    "Helper session is not authorized. "
                    "Regenerate HELPER_SESSION with scripts/generate_session.py."
                )
                await self._client.disconnect()
                self._client = None
                return
            self._me = await self._client.get_me()
            self._ready = True
            logger.info("Helper userbot authorized as id=%s @%s", self._me.id, self.username)
        except Exception as exc:
            logger.error("Helper userbot failed to start: %s", exc)
            self._ready = False
            if self._client is not None:
                try:
                    await self._client.disconnect()
                finally:
                    self._client = None

    async def stop(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self._ready = False

    # ---------- access management ----------

    def _manual_text(self) -> str:
        who = f"@{self.username}" if self.username else "the helper account"
        return (
            "I couldn't finish helper setup automatically. To approve requests that "
            f"were submitted before I became admin, add {who} as an administrator "
            "of the chat with the \u201cAdd Members / Invite Users\u201d permission, "
            "then tap Approve again."
        )

    async def ensure_access(self, bot, chat_id: int) -> AccessResult:
        """Make sure the helper is an admin of ``chat_id``.

        Tries to add + promote the helper using the bot's own admin rights.
        Falls back to manual instructions when Telegram does not permit it.
        """
        if not self.ready:
            return AccessResult(False, "disabled", "Helper is not configured.")

        helper_id = self._me.id

        # 1. Already an admin?
        try:
            member = await bot.get_chat_member(chat_id, helper_id)
            status = getattr(member, "status", None)
        except Exception:
            status = None

        if status in ("administrator", "creator"):
            return AccessResult(True, "ok", "Helper already has admin access.", self.username)

        # 2. Make sure the helper is at least a member.
        if status not in ("member", "restricted"):
            joined = await self._join_via_bot(bot, chat_id)
            if not joined:
                return AccessResult(False, "manual", self._manual_text(), self.username)

        # 3. Promote (needs the bot's own can_promote_members right).
        try:
            await bot.promote_chat_member(
                chat_id,
                helper_id,
                can_manage_chat=True,
                can_invite_users=True,
            )
            return AccessResult(True, "ok", "Helper connected.", self.username)
        except Exception as exc:
            logger.info("Helper promote failed for chat %s: %s", chat_id, exc)
            return AccessResult(False, "manual", self._manual_text(), self.username)

    async def _join_via_bot(self, bot, chat_id: int) -> bool:
        """Have the helper join the chat using an invite link the bot creates."""
        link = None
        try:
            created = await bot.create_chat_invite_link(
                chat_id, creates_join_request=False
            )
            link = created.invite_link
        except Exception as exc:
            logger.info("create_chat_invite_link failed for %s: %s", chat_id, exc)
            try:
                link = await bot.export_chat_invite_link(chat_id)
            except Exception as exc2:
                logger.info("export_chat_invite_link failed for %s: %s", chat_id, exc2)
                return False

        invite = _invite_hash(link)
        if not invite:
            return False
        try:
            async with self._lock:
                await self._client(ImportChatInviteRequest(invite))
            return True
        except errors.UserAlreadyParticipantError:
            return True
        except errors.FloodWaitError as exc:
            logger.warning("Helper join flood-wait %ss for chat %s", exc.seconds, chat_id)
            return False
        except Exception as exc:
            logger.info("Helper join failed for %s: %s", chat_id, exc)
            return False

    # ---------- MTProto primitives (each holds the lock briefly) ----------

    async def _resolve_peer(self, chat_id: int):
        async with self._lock:
            try:
                return await self._client.get_input_entity(chat_id)
            except (ValueError, TypeError):
                return await self._client.get_entity(chat_id)

    async def _count_requests(self, peer) -> int:
        async with self._lock:
            res = await self._client(
                GetChatInviteImportersRequest(
                    peer=peer,
                    offset_date=None,
                    offset_user=InputUserEmpty(),
                    limit=1,
                    requested=True,
                )
            )
        return int(res.count)

    async def _fetch_requests(self, peer, limit: int):
        async with self._lock:
            return await self._client(
                GetChatInviteImportersRequest(
                    peer=peer,
                    offset_date=None,
                    offset_user=InputUserEmpty(),
                    limit=limit,
                    requested=True,
                )
            )

    async def _hide_all(self, peer) -> None:
        async with self._lock:
            await self._client(
                HideAllChatJoinRequestsRequest(peer=peer, approved=True)
            )

    async def _hide_one(self, peer, target) -> None:
        async with self._lock:
            await self._client(
                HideChatJoinRequestRequest(peer=peer, user_id=target, approved=True)
            )

    @staticmethod
    def _input_user(user_id: int, user):
        if user is not None and getattr(user, "access_hash", None) is not None:
            return InputUser(user.id, user.access_hash)
        return user_id

    @staticmethod
    async def _sleep_cancellable(seconds: float, cancelled: Optional[CancelCb]) -> None:
        """Sleep without holding the MTProto lock, waking early on cancel."""
        end = time.monotonic() + seconds
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            if cancelled is not None and cancelled():
                return
            await asyncio.sleep(min(1.0, remaining))

    @staticmethod
    def _is_recoverable(exc: BaseException) -> bool:
        """True for transient failures worth waiting out and retrying.

        Covers Python transport failures (timeouts, dropped connections) and
        Telegram's temporary server-side/RPC errors, including Telethon's
        "Request was unsuccessful N time(s)" once its own retries are exhausted.
        """
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
            return True
        if TELETHON_AVAILABLE:
            for cls_name in (
                "ServerError",
                "TimedOutError",
                "TimeoutError",
                "RpcCallFailError",
                "RpcMcgetFailError",
            ):
                cls = getattr(errors, cls_name, None)
                if cls is not None and isinstance(exc, cls):
                    return True
        msg = str(exc).lower()
        return "request was unsuccessful" in msg or "timeout" in msg or "timed out" in msg

    @staticmethod
    def _backoff(attempt: int) -> int:
        """Exponential backoff in seconds for the Nth consecutive retry."""
        return min(2 ** attempt, MAX_BACKOFF)

    async def _ensure_connected(self) -> None:
        """Reconnect the Telethon client if the connection has dropped."""
        client = self._client
        if client is None:
            return
        try:
            if not client.is_connected():
                logger.info("Helper reconnecting after dropped connection…")
                await client.connect()
        except Exception as exc:
            logger.warning("Helper reconnect attempt failed: %s", exc)

    # ---------- public reads ----------

    async def get_pending_count(self, chat_id: int) -> Optional[int]:
        """Actual number of pending requests on Telegram, or None if unavailable."""
        if not self.ready:
            return None
        try:
            peer = await self._resolve_peer(chat_id)
            return await self._count_requests(peer)
        except errors.ChatAdminRequiredError:
            return None
        except Exception as exc:
            logger.info("get_pending_count failed for %s: %s", chat_id, exc)
            return None

    # ---------- streaming approvers ----------

    async def approve_all_stream(
        self,
        chat_id: int,
        progress: Optional[ProgressCb] = None,
        cancelled: Optional[CancelCb] = None,
    ) -> ApproveResult:
        """Continuously approve *every* pending request until the queue is empty.

        Telegram approves join requests in server-side chunks, so a single
        ``HideAll`` call clears only part of a large queue. We loop, recounting
        between calls to measure real progress, until nothing remains (or a
        genuine stop condition is hit). FloodWaits are waited out and the run
        resumes automatically.
        """
        if not self.ready:
            return ApproveResult(note="Helper is not configured.")

        approved = 0
        try:
            peer = await self._resolve_peer(chat_id)
            remaining = await self._count_requests(peer)
            total = remaining
            if remaining == 0:
                return ApproveResult(note="No pending requests on Telegram.")
            if progress is not None:
                await progress(approved, total)

            stagnant = 0
            errors_in_a_row = 0
            while remaining > 0:
                if cancelled is not None and cancelled():
                    return ApproveResult(approved=approved, cancelled=True)

                # ---- clear a server-side chunk ----
                hide_ok = False
                try:
                    await self._hide_all(peer)
                    hide_ok = True
                    errors_in_a_row = 0
                except errors.FloodWaitError as exc:
                    if exc.seconds > MAX_FLOOD_WAIT:
                        return ApproveResult(
                            approved=approved,
                            note=(
                                f"Paused — Telegram flood-wait of {exc.seconds}s. "
                                "Approved so far are done; tap Approve All again "
                                "later to continue."
                            ),
                        )
                    logger.warning(
                        "approve_all flood-wait chat=%s wait=%ss approved=%s remaining=%s",
                        chat_id, exc.seconds, approved, remaining,
                    )
                    await self._sleep_cancellable(exc.seconds + 1, cancelled)
                    continue
                except Exception as exc:
                    if not self._is_recoverable(exc):
                        raise
                    errors_in_a_row += 1
                    if errors_in_a_row > MAX_RECOVERABLE_RETRIES:
                        logger.error(
                            "approve_all pausing chat=%s after %s consecutive "
                            "recoverable errors; approved=%s remaining=%s last=%r",
                            chat_id, errors_in_a_row, approved, remaining, exc,
                        )
                        return ApproveResult(
                            approved=approved,
                            note=(
                                "Paused after repeated Telegram timeouts. Approved "
                                "so far are done; tap Approve All again to continue "
                                "from what's still pending."
                            ),
                        )
                    wait = self._backoff(errors_in_a_row)
                    logger.warning(
                        "approve_all recoverable error chat=%s retry=%s/%s wait=%ss "
                        "approved=%s remaining=%s exc=%r",
                        chat_id, errors_in_a_row, MAX_RECOVERABLE_RETRIES, wait,
                        approved, remaining, exc,
                    )
                    await self._sleep_cancellable(wait, cancelled)
                    if cancelled is not None and cancelled():
                        return ApproveResult(approved=approved, cancelled=True)
                    await self._ensure_connected()
                    # fall through: recount below to credit anything that landed

                # ---- settle, then recount to measure real progress ----
                await asyncio.sleep(0.7)
                try:
                    new_remaining = await self._count_requests(peer)
                except Exception as exc:
                    if not self._is_recoverable(exc):
                        raise
                    errors_in_a_row += 1
                    if errors_in_a_row > MAX_RECOVERABLE_RETRIES:
                        logger.error(
                            "approve_all pausing chat=%s after %s consecutive "
                            "recount errors; approved=%s last=%r",
                            chat_id, errors_in_a_row, approved, exc,
                        )
                        return ApproveResult(
                            approved=approved,
                            note=(
                                "Paused after repeated Telegram timeouts. Approved "
                                "so far are done; tap Approve All again to continue."
                            ),
                        )
                    wait = self._backoff(errors_in_a_row)
                    logger.warning(
                        "approve_all recount error chat=%s retry=%s/%s wait=%ss "
                        "approved=%s remaining=%s exc=%r",
                        chat_id, errors_in_a_row, MAX_RECOVERABLE_RETRIES, wait,
                        approved, remaining, exc,
                    )
                    await self._sleep_cancellable(wait, cancelled)
                    await self._ensure_connected()
                    continue

                # Credit whatever actually drained — even if the hide call itself
                # timed out, some approvals may have landed server-side.
                delta = remaining - new_remaining
                remaining = new_remaining
                if delta > 0:
                    approved += delta
                    total = max(total, approved + remaining)
                    # Real progress means we're not genuinely stuck: forgive the
                    # earlier timeouts so intermittent errors never pause a run
                    # that is still draining.
                    errors_in_a_row = 0
                    if progress is not None:
                        await progress(approved, total)

                # Stagnation only counts against clean calls that cleared nothing;
                # a recovered timeout should not trip the "drained" heuristic.
                if hide_ok:
                    if delta > 0:
                        stagnant = 0
                    else:
                        stagnant += 1
                        if stagnant >= 3:
                            break

            return ApproveResult(approved=approved)
        except errors.ChatAdminRequiredError:
            return ApproveResult(approved=approved, note=self._manual_text())
        except Exception as exc:
            logger.info("approve_all_stream failed for %s: %s", chat_id, exc)
            return ApproveResult(
                approved=approved,
                note="Telegram returned an error while approving.",
            )

    async def approve_amount_stream(
        self,
        chat_id: int,
        amount: int,
        progress: Optional[ProgressCb] = None,
        cancelled: Optional[CancelCb] = None,
    ) -> ApproveResult:
        """Approve exactly up to ``amount`` requests, oldest first.

        Runs independently of :meth:`approve_all_stream`; approves individually
        so the exact requested number is honoured. FloodWaits are waited out and
        resumed automatically up to :data:`MAX_FLOOD_WAIT`.
        """
        if not self.ready:
            return ApproveResult(note="Helper is not configured.")

        approved: list[int] = []
        failed = 0
        fetch_errors = 0
        try:
            peer = await self._resolve_peer(chat_id)
            if progress is not None:
                await progress(0, amount)

            while len(approved) < amount:
                if cancelled is not None and cancelled():
                    return ApproveResult(
                        approved=len(approved),
                        failed=failed,
                        user_ids=approved,
                        cancelled=True,
                    )

                try:
                    batch = await self._fetch_requests(
                        peer, limit=min(100, amount - len(approved))
                    )
                    fetch_errors = 0
                except errors.FloodWaitError as exc:
                    if exc.seconds > MAX_FLOOD_WAIT:
                        return ApproveResult(
                            approved=len(approved),
                            failed=failed,
                            user_ids=approved,
                            note=(
                                "Paused — Telegram asked for a long wait. Approved "
                                "so far are done; run it again later to continue."
                            ),
                        )
                    logger.warning(
                        "approve_amount fetch flood-wait chat=%s wait=%ss approved=%s",
                        chat_id, exc.seconds, len(approved),
                    )
                    await self._sleep_cancellable(exc.seconds + 1, cancelled)
                    continue
                except Exception as exc:
                    if not self._is_recoverable(exc):
                        raise
                    fetch_errors += 1
                    if fetch_errors > MAX_RECOVERABLE_RETRIES:
                        logger.error(
                            "approve_amount pausing chat=%s after %s consecutive "
                            "fetch errors; approved=%s last=%r",
                            chat_id, fetch_errors, len(approved), exc,
                        )
                        return ApproveResult(
                            approved=len(approved),
                            failed=failed,
                            user_ids=approved,
                            note=(
                                "Paused after repeated Telegram timeouts. Approved "
                                "so far are done; run it again to continue."
                            ),
                        )
                    wait = self._backoff(fetch_errors)
                    logger.warning(
                        "approve_amount fetch recoverable error chat=%s retry=%s/%s "
                        "wait=%ss approved=%s exc=%r",
                        chat_id, fetch_errors, MAX_RECOVERABLE_RETRIES, wait,
                        len(approved), exc,
                    )
                    await self._sleep_cancellable(wait, cancelled)
                    await self._ensure_connected()
                    continue

                if not batch.importers:
                    break

                users = {u.id: u for u in batch.users}
                made_progress = False
                for imp in batch.importers:
                    if len(approved) >= amount:
                        break
                    if cancelled is not None and cancelled():
                        return ApproveResult(
                            approved=len(approved),
                            failed=failed,
                            user_ids=approved,
                            cancelled=True,
                        )

                    target = self._input_user(imp.user_id, users.get(imp.user_id))
                    outcome = await self._approve_one(peer, target, cancelled)
                    if outcome is True:
                        approved.append(imp.user_id)
                        made_progress = True
                        if progress is not None and len(approved) % 20 == 0:
                            await progress(len(approved), amount)
                    elif outcome == "cancelled":
                        return ApproveResult(
                            approved=len(approved),
                            failed=failed,
                            user_ids=approved,
                            cancelled=True,
                        )
                    elif outcome == "flood":
                        if progress is not None:
                            await progress(len(approved), amount)
                        return ApproveResult(
                            approved=len(approved),
                            failed=failed,
                            user_ids=approved,
                            note=(
                                "Paused — Telegram asked for a long wait. "
                                "Approved so far are done; run it again later "
                                "to continue."
                            ),
                        )
                    else:
                        failed += 1

                if not made_progress:
                    # Everything in this page failed and stayed at the top of the
                    # queue; stop rather than refetch the same rows forever.
                    break

            if progress is not None:
                await progress(len(approved), amount)
            return ApproveResult(
                approved=len(approved), failed=failed, user_ids=approved
            )
        except errors.ChatAdminRequiredError:
            return ApproveResult(
                approved=len(approved),
                failed=failed,
                user_ids=approved,
                note=self._manual_text(),
            )
        except Exception as exc:
            logger.info("approve_amount_stream failed for %s: %s", chat_id, exc)
            return ApproveResult(
                approved=len(approved),
                failed=failed,
                user_ids=approved,
                note="Telegram returned an error while approving.",
            )

    async def _approve_one(self, peer, target, cancelled: Optional[CancelCb]):
        """Approve one request. Returns True | False | 'cancelled' | 'flood'."""
        attempt = 0
        while True:
            try:
                await self._hide_one(peer, target)
                return True
            except errors.FloodWaitError as exc:
                if exc.seconds > MAX_FLOOD_WAIT:
                    return "flood"
                await self._sleep_cancellable(exc.seconds + 1, cancelled)
                if cancelled is not None and cancelled():
                    return "cancelled"
                # loop and retry the same user
            except Exception as exc:
                if self._is_recoverable(exc):
                    attempt += 1
                    if attempt > MAX_RECOVERABLE_RETRIES:
                        logger.warning(
                            "approve one giving up target=%s after %s retries: %r",
                            target, attempt, exc,
                        )
                        return False
                    wait = self._backoff(attempt)
                    logger.warning(
                        "approve one recoverable error target=%s retry=%s/%s "
                        "wait=%ss exc=%r",
                        target, attempt, MAX_RECOVERABLE_RETRIES, wait, exc,
                    )
                    await self._sleep_cancellable(wait, cancelled)
                    if cancelled is not None and cancelled():
                        return "cancelled"
                    await self._ensure_connected()
                    continue
                logger.info("approve one failed (%s): %s", target, exc)
                return False


async def resolve_pending_count(
    helper: Optional[HelperUserbot], db, channel
) -> tuple[int, bool]:
    """Return ``(count, is_live)`` — the true Telegram count when the helper can
    reach the chat, otherwise the locally-tracked count."""
    if helper is not None and helper.ready:
        live = await helper.get_pending_count(channel["chat_id"])
        if live is not None:
            return live, True
    return await db.count_pending(channel["chat_id"]), False
