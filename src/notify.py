"""Telegram delivery and command handling. Phase 2, extended in phase 4.

Setup:
    1. Message @BotFather on Telegram, send /newbot, follow the prompts.
       Put the token in .env as TELEGRAM_BOT_TOKEN.
    2. Message @userinfobot to get your numeric chat id. Put it in .env.
    3. Message your own bot once before it can message you. Telegram does not allow
       bots to initiate conversations.

Bot polling works from behind home NAT with no port forwarding, which is why this
approach beats a webhook for a machine sitting in a dorm room.

SECURITY: check that the incoming chat id matches TELEGRAM_CHAT_ID before acting on
any command. Anyone who finds your bot username can message it.
"""

from src import config


def send(text: str) -> None:
    """Send a message to the configured chat."""
    raise NotImplementedError("phase 2")


def run_bot() -> None:
    """Start polling for incoming commands. Blocks.

    Handlers to register (phase 4):
        /lift /court /cond /mob   log a training session
        /pain                     daily injury check-in
        /injury                   open or resolve an injury
        /brief                    send the briefing on demand
        /status                   what the assistant currently knows
    """
    raise NotImplementedError("phase 2")
