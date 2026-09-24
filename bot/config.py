import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_id: int
    required_channel: str
    required_channel_url: str
    database_path: str
    # Optional Telethon helper userbot (for historical join-request approval).
    helper_api_id: Optional[int]
    helper_api_hash: Optional[str]
    helper_session: Optional[str]

    @property
    def helper_configured(self) -> bool:
        return bool(self.helper_api_id and self.helper_api_hash and self.helper_session)


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


def load_config() -> Config:
    return Config(
        bot_token=_require("BOT_TOKEN"),
        admin_id=int(_require("ADMIN_ID")),
        required_channel=_require("REQUIRED_CHANNEL"),
        required_channel_url=os.getenv(
            "REQUIRED_CHANNEL_URL", "https://t.me/proacceptrequests"
        ),
        database_path=os.getenv("DATABASE_PATH", "data/pro_accept_requests.db"),
        helper_api_id=int(os.environ["HELPER_API_ID"])
        if os.getenv("HELPER_API_ID")
        else None,
        helper_api_hash=os.getenv("HELPER_API_HASH") or None,
        helper_session=os.getenv("HELPER_SESSION") or None,
    )
