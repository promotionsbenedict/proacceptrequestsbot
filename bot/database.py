import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

# Free plan limit; premium users are unlimited.
FREE_CHANNEL_LIMIT = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thin async wrapper around a single SQLite connection."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    # ---------- lifecycle ----------

    async def connect(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
        await self._create_schema()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database is not connected. Call connect() first.")
        return self._db

    async def _create_schema(self) -> None:
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                plan        TEXT NOT NULL DEFAULT 'free',
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channels (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id        INTEGER NOT NULL,
                chat_id         INTEGER NOT NULL,
                title           TEXT,
                type            TEXT,
                is_active       INTEGER NOT NULL DEFAULT 0,
                welcome_enabled INTEGER NOT NULL DEFAULT 1,
                welcome_text    TEXT,
                welcome_image   TEXT,
                welcome_buttons TEXT,
                goodbye_enabled INTEGER NOT NULL DEFAULT 0,
                goodbye_text    TEXT,
                goodbye_image   TEXT,
                goodbye_buttons TEXT,
                created_at      TEXT NOT NULL,
                UNIQUE(owner_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS pending_requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                first_name  TEXT,
                username    TEXT,
                created_at  TEXT NOT NULL,
                UNIQUE(chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS broadcast_users (
                chat_id     INTEGER PRIMARY KEY,
                first_name  TEXT,
                username    TEXT,
                opted_in    INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS promoted_channels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                url         TEXT NOT NULL,
                position    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                plan        TEXT NOT NULL,
                amount      REAL,
                currency    TEXT,
                provider    TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT NOT NULL
            );
            """
        )
        await self.db.commit()

    # ---------- users ----------

    async def upsert_user(
        self, user_id: int, username: Optional[str], first_name: Optional[str]
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO users (id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name, _now()),
        )
        await self.db.commit()

    async def get_user(self, user_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return await cur.fetchone()

    async def set_plan(self, user_id: int, plan: str) -> None:
        await self.db.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, user_id))
        await self.db.commit()

    async def is_premium(self, user_id: int) -> bool:
        user = await self.get_user(user_id)
        return bool(user) and user["plan"] == "premium"

    async def count_users(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) AS c FROM users")
        return (await cur.fetchone())["c"]

    # ---------- channels ----------

    async def add_channel(
        self,
        owner_id: int,
        chat_id: int,
        title: Optional[str],
        chat_type: Optional[str],
    ) -> int:
        cur = await self.db.execute(
            """
            INSERT INTO channels (owner_id, chat_id, title, type, welcome_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, chat_id) DO UPDATE SET
                title = excluded.title,
                type = excluded.type
            RETURNING id
            """,
            (
                owner_id,
                chat_id,
                title,
                chat_type,
                "Welcome, {first_name}! Your request to join {chat_title} has been approved.",
                _now(),
            ),
        )
        row = await cur.fetchone()
        await self.db.commit()
        return row["id"]

    async def get_channel(self, channel_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.db.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
        return await cur.fetchone()

    async def get_channel_by_chat(self, chat_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM channels WHERE chat_id = ?", (chat_id,)
        )
        return await cur.fetchone()

    async def list_channels(self, owner_id: int) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM channels WHERE owner_id = ? ORDER BY created_at",
            (owner_id,),
        )
        return list(await cur.fetchall())

    async def count_channels(self, owner_id: int) -> int:
        cur = await self.db.execute(
            "SELECT COUNT(*) AS c FROM channels WHERE owner_id = ?", (owner_id,)
        )
        return (await cur.fetchone())["c"]

    async def count_all_channels(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) AS c FROM channels")
        return (await cur.fetchone())["c"]

    async def set_channel_active(self, channel_id: int, active: bool) -> None:
        await self.db.execute(
            "UPDATE channels SET is_active = ? WHERE id = ?",
            (1 if active else 0, channel_id),
        )
        await self.db.commit()

    async def update_channel_field(self, channel_id: int, field: str, value: Any) -> None:
        allowed = {
            "welcome_enabled",
            "welcome_text",
            "welcome_image",
            "welcome_buttons",
            "goodbye_enabled",
            "goodbye_text",
            "goodbye_image",
            "goodbye_buttons",
        }
        if field not in allowed:
            raise ValueError(f"Illegal channel field: {field}")
        await self.db.execute(
            f"UPDATE channels SET {field} = ? WHERE id = ?", (value, channel_id)
        )
        await self.db.commit()

    async def delete_channel(self, channel_id: int) -> None:
        channel = await self.get_channel(channel_id)
        if channel:
            await self.db.execute(
                "DELETE FROM pending_requests WHERE chat_id = ?", (channel["chat_id"],)
            )
        await self.db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        await self.db.commit()

    # ---------- pending join requests ----------

    async def add_pending(
        self,
        chat_id: int,
        user_id: int,
        first_name: Optional[str],
        username: Optional[str],
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO pending_requests (chat_id, user_id, first_name, username, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO NOTHING
            """,
            (chat_id, user_id, first_name, username, _now()),
        )
        await self.db.commit()

    async def list_pending(self, chat_id: int) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM pending_requests WHERE chat_id = ? ORDER BY created_at",
            (chat_id,),
        )
        return list(await cur.fetchall())

    async def count_pending(self, chat_id: int) -> int:
        cur = await self.db.execute(
            "SELECT COUNT(*) AS c FROM pending_requests WHERE chat_id = ?", (chat_id,)
        )
        return (await cur.fetchone())["c"]

    async def clear_pending(self, chat_id: int) -> None:
        await self.db.execute(
            "DELETE FROM pending_requests WHERE chat_id = ?", (chat_id,)
        )
        await self.db.commit()

    async def remove_pending(self, chat_id: int, user_id: int) -> None:
        await self.db.execute(
            "DELETE FROM pending_requests WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        await self.db.commit()

    # ---------- broadcast opt-ins ----------

    async def add_broadcast_user(
        self,
        chat_id: int,
        first_name: Optional[str],
        username: Optional[str],
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO broadcast_users (chat_id, first_name, username, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username,
                opted_in = 1
            """,
            (chat_id, first_name, username, _now()),
        )
        await self.db.commit()

    async def opt_out_broadcast(self, chat_id: int) -> None:
        await self.db.execute(
            "UPDATE broadcast_users SET opted_in = 0 WHERE chat_id = ?", (chat_id,)
        )
        await self.db.commit()

    async def list_broadcast_users(self) -> list[int]:
        cur = await self.db.execute(
            "SELECT chat_id FROM broadcast_users WHERE opted_in = 1"
        )
        return [row["chat_id"] for row in await cur.fetchall()]

    # ---------- promoted channels ----------

    async def add_promoted(self, name: str, url: str) -> None:
        cur = await self.db.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS pos FROM promoted_channels"
        )
        position = (await cur.fetchone())["pos"]
        await self.db.execute(
            "INSERT INTO promoted_channels (name, url, position, created_at) VALUES (?, ?, ?, ?)",
            (name, url, position, _now()),
        )
        await self.db.commit()

    async def list_promoted(self) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM promoted_channels ORDER BY position, id"
        )
        return list(await cur.fetchall())

    async def delete_promoted(self, promoted_id: int) -> None:
        await self.db.execute(
            "DELETE FROM promoted_channels WHERE id = ?", (promoted_id,)
        )
        await self.db.commit()

    # ---------- payments (scaffold for future monetization) ----------

    async def record_payment(
        self,
        user_id: int,
        plan: str,
        amount: Optional[float],
        currency: Optional[str],
        provider: Optional[str],
        status: str = "pending",
    ) -> int:
        cur = await self.db.execute(
            """
            INSERT INTO payments (user_id, plan, amount, currency, provider, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (user_id, plan, amount, currency, provider, status, _now()),
        )
        row = await cur.fetchone()
        await self.db.commit()
        return row["id"]


# ---------- JSON helpers for inline button storage ----------


def encode_buttons(buttons: list[dict]) -> str:
    return json.dumps(buttons, ensure_ascii=False)


def decode_buttons(raw: Optional[str]) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
