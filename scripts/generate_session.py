"""Generate a Telethon StringSession for the helper userbot.

Run this ONCE, locally, on a trusted machine, signed in as the dedicated helper
account (not your personal account):

    python scripts/generate_session.py

It will ask for the API ID / hash (from https://my.telegram.org) and log you in
via phone number + code (and 2FA password if enabled). Copy the printed string
into the HELPER_SESSION environment variable. Treat it like a password.
"""

from telethon import TelegramClient
from telethon.sessions import StringSession


def main() -> None:
    api_id = int(input("API ID: ").strip())
    api_hash = input("API hash: ").strip()

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\nYour HELPER_SESSION string (keep it secret):\n")
        print(client.session.save())


if __name__ == "__main__":
    main()
