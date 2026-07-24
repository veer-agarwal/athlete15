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

import requests

from src import config

API_BASE = "https://api.telegram.org/bot"

# Telegram rejects any message body over 4096 UTF-16 code units. The briefing will
# grow past that once WHOOP, Notion, and news are all in, so split rather than let
# a long message fail with a 400 at 7:00 AM.
MAX_MESSAGE_CHARS = 4096


def _split(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Break text into chunks under the limit, preferring line boundaries."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        # A single line longer than the limit cannot be kept whole. Hard slice it.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]

        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def send(text: str) -> None:
    """Send a message to the configured chat.

    Sent as plain text with no parse_mode. Telegram's MarkdownV2 requires escaping
    a long list of characters including - . ( ) ! and would reject briefing lines
    like "high 84 low 61" or any URL. Formatting is not worth a silently failed
    briefing; revisit only if the message ever needs bold or links.

    Uses requests rather than python-telegram-bot because PTB's API is async, and
    this is called from the synchronous APScheduler job. run_bot() will use PTB.

    Raises:
        RuntimeError: if the token or chat id is missing, or Telegram rejects the
            message (its JSON body carries the reason, which HTTPError would hide).
        requests.Timeout: if a request exceeds 10 seconds.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")
    if not config.TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set in .env")

    url = f"{API_BASE}{config.TELEGRAM_BOT_TOKEN}/sendMessage"

    for chunk in _split(text):
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        # timeout is not optional. Without it a hung connection blocks the whole
        # morning job, since scheduler and bot share one process.
        response = requests.post(url, json=payload, timeout=10)

        # Telegram answers non-2xx with a JSON body explaining why ("chat not
        # found", "bot was blocked by the user"). raise_for_status() alone throws
        # that away and leaves you staring at a bare 400.
        try:
            body = response.json()
        except ValueError:
            response.raise_for_status()
            raise RuntimeError(f"telegram returned unparseable body: {response.text}")

        if not body.get("ok"):
            raise RuntimeError(
                f"telegram error {body.get('error_code')}: {body.get('description')}"
            )


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


if __name__ == "__main__":
    # Run directly to test delivery alone:  python -m src.notify
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")
    send(f"athlete15 test message\nsent {now}")
    print(f"sent to chat {config.TELEGRAM_CHAT_ID}")
