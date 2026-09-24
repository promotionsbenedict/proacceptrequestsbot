import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_id: int
    required_channel: str
    required_channel_url: str
    database_path: str


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
    )
