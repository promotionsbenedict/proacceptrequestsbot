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
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

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
        # Serialize MTProto calls; a single user session is not meant for
        # heavy concurrency and this keeps flood-wait behaviour predictable.
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

    # ---------- MTProto join-request operations ----------

    async def _peer(self, chat_id: int):
        try:
            return await self._client.get_input_entity(chat_id)
        except (ValueError, TypeError):
            return await self._client.get_entity(chat_id)

    async def get_pending_count(self, chat_id: int) -> Optional[int]:
        """Actual number of pending requests on Telegram, or None if unavailable."""
        if not self.ready:
            return None
        try:
            async with self._lock:
                peer = await self._peer(chat_id)
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
        except errors.ChatAdminRequiredError:
            return None
        except Exception as exc:
            logger.info("get_pending_count failed for %s: %s", chat_id, exc)
            return None

    async def approve_all(self, chat_id: int) -> ApproveResult:
        """Approve every pending request in one server-side call."""
        if not self.ready:
            return ApproveResult(note="Helper is not configured.")
        try:
            async with self._lock:
                peer = await self._peer(chat_id)
                res = await self._client(
                    GetChatInviteImportersRequest(
                        peer=peer,
                        offset_date=None,
                        offset_user=InputUserEmpty(),
                        limit=1,
                        requested=True,
                    )
                )
                total = int(res.count)
                if total == 0:
                    return ApproveResult(note="No pending requests on Telegram.")
                await self._client(
                    HideAllChatJoinRequestsRequest(peer=peer, approved=True)
                )
            return ApproveResult(approved=total)
        except errors.ChatAdminRequiredError:
            return ApproveResult(note=self._manual_text())
        except errors.FloodWaitError as exc:
            return ApproveResult(
                note=f"Telegram asked me to wait {exc.seconds}s. Please try again shortly."
            )
        except Exception as exc:
            logger.info("approve_all failed for %s: %s", chat_id, exc)
            return ApproveResult(note="Telegram returned an error while approving.")

    async def approve_amount(self, chat_id: int, amount: int) -> ApproveResult:
        """Approve up to ``amount`` pending requests, oldest batch first.

        Approved requests disappear from the queue, so we simply keep fetching
        from the top until we've approved enough or run out.
        """
        if not self.ready:
            return ApproveResult(note="Helper is not configured.")
        approved: list[int] = []
        failed = 0
        try:
            async with self._lock:
                peer = await self._peer(chat_id)
                while len(approved) < amount:
                    remaining = amount - len(approved)
                    res = await self._client(
                        GetChatInviteImportersRequest(
                            peer=peer,
                            offset_date=None,
                            offset_user=InputUserEmpty(),
                            limit=min(100, remaining),
                            requested=True,
                        )
                    )
                    if not res.importers:
                        break
                    users = {u.id: u for u in res.users}
                    made_progress = False
                    for imp in res.importers:
                        if len(approved) >= amount:
                            break
                        user = users.get(imp.user_id)
                        target = (
                            InputUser(user.id, user.access_hash)
                            if user is not None and user.access_hash is not None
                            else imp.user_id
                        )
                        try:
                            await self._client(
                                HideChatJoinRequestRequest(
                                    peer=peer, user_id=target, approved=True
                                )
                            )
                            approved.append(imp.user_id)
                            made_progress = True
                        except errors.FloodWaitError as exc:
                            if exc.seconds <= 20:
                                await asyncio.sleep(exc.seconds + 1)
                                try:
                                    await self._client(
                                        HideChatJoinRequestRequest(
                                            peer=peer, user_id=target, approved=True
                                        )
                                    )
                                    approved.append(imp.user_id)
                                    made_progress = True
                                except Exception:
                                    failed += 1
                            else:
                                return ApproveResult(
                                    approved=len(approved),
                                    failed=failed,
                                    user_ids=approved,
                                    note=(
                                        f"Stopped early — Telegram flood-wait of "
                                        f"{exc.seconds}s. Approved so far are done; "
                                        "run it again later to continue."
                                    ),
                                )
                        except Exception as exc:
                            logger.info("approve one failed (%s): %s", imp.user_id, exc)
                            failed += 1
                    if not made_progress:
                        # Everything left in this batch failed; avoid looping forever.
                        break
            return ApproveResult(approved=len(approved), failed=failed, user_ids=approved)
        except errors.ChatAdminRequiredError:
            return ApproveResult(
                approved=len(approved),
                failed=failed,
                user_ids=approved,
                note=self._manual_text(),
            )
        except Exception as exc:
            logger.info("approve_amount failed for %s: %s", chat_id, exc)
            return ApproveResult(
                approved=len(approved),
                failed=failed,
                user_ids=approved,
                note="Telegram returned an error while approving.",
            )


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
