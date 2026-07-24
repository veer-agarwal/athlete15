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

import logging

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src import config, db, training

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


HELP_TEXT = (
    "athlete15\n"
    "\n"
    "Send plain text to log. No command to remember:\n"
    "  right shoulder 3\n"
    "  court 90 rpe 6\n"
    "  lifted 60 min rpe 8\n"
    "  tweaked my left ankle\n"
    "  right shoulder resolved\n"
    "\n"
    "Everything you send is stored word for word before it is parsed, so a\n"
    "misread never loses the entry. The reply says what was understood.\n"
    "\n"
    "/status  active injuries and current load\n"
    "/help    this message"
)


def _is_authorized(update: Update) -> bool:
    """Whether an update came from the configured chat.

    Anyone who guesses the bot username can message it, and the bot writes to the
    database, so this is checked on every handler rather than once at startup.
    Rejections are logged rather than silently dropped: an unexpected chat id
    showing up is worth being able to see.
    """
    chat = update.effective_chat
    if chat is None:
        return False
    if str(chat.id) != str(config.TELEGRAM_CHAT_ID):
        logging.warning("rejected update from chat id %s", chat.id)
        return False
    return True


async def _on_text(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any non-command text is a log entry.

    Order matters and is the whole point of this handler: the raw text goes to
    disk first, then parsing runs, then the parse result is attached to the row
    that already exists. A crash or a parser bug anywhere after the first write
    costs you a parse, never the entry.

    The database calls are synchronous inside an async handler. They are local
    SQLite writes on the order of a millisecond, so blocking the event loop for
    that long is not worth the complexity of a thread pool. Revisit if a handler
    ever does real network work.
    """
    if not _is_authorized(update):
        return

    message = update.effective_message
    if message is None or not message.text:
        return

    entry_id = db.insert_log_entry(message.text)
    logging.info("log entry #%d received, %d chars", entry_id, len(message.text))

    try:
        result = training.parse_entry(message.text)
        applied = training.apply_entry(result)
        db.record_parse(
            entry_id, result, result["parse_status"], result["parse_method"]
        )
        reply = training.format_parse_reply(result, applied, entry_id)
    except Exception:
        # The entry is already safe on disk. Mark it so a later re-parse can find
        # it, tell the truth in the reply, and do not raise: an exception escaping
        # here would be swallowed by PTB and look like the bot ignoring you.
        logging.exception("parsing entry #%d failed", entry_id)
        db.record_parse(entry_id, None, "error", training.DEFAULT_PARSE_METHOD)
        reply = (
            f"saved entry #{entry_id} but parsing it failed.\n"
            "the raw text is stored and nothing was lost."
        )

    await message.reply_text(reply)


async def _on_status(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Current training load and open injuries, on demand."""
    if not _is_authorized(update):
        return
    await update.effective_message.reply_text(training.format_training_block())


async def _on_help(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.effective_message.reply_text(HELP_TEXT)


async def _on_unknown_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Catch slash commands that are not registered.

    Without this, a mistyped command is silently dropped. Worse, it is NOT stored
    as a log entry (commands never are), so "/court 90 rpe 6" typed out of habit
    from the old command grammar would vanish entirely. Say so explicitly.
    """
    if not _is_authorized(update):
        return
    await update.effective_message.reply_text(
        "unknown command, and commands are not logged.\n"
        "send it again without the slash to log it. /help for examples"
    )


def run_bot() -> None:
    """Start polling for incoming messages. Blocks.

    Polling rather than a webhook because this runs behind dorm NAT with no port
    forwarding and no public hostname.

    Raises:
        RuntimeError: if the token or chat id is missing. Starting without the
            chat id would leave the authorization check comparing against an
            empty string and rejecting everything, which looks like a dead bot.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")
    if not config.TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set in .env")

    # python-telegram-bot talks over httpx, which logs every request URL at INFO.
    # Telegram puts the bot token in the URL path, so leaving this on writes the
    # token into brief.log on every poll, forever. Anyone with that token owns
    # the bot. Warnings and errors still come through.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "help"], _on_help))
    app.add_handler(CommandHandler("status", _on_status))
    # Text first, then the catch-all for commands. PTB dispatches to the first
    # matching handler in a group, so the unknown-command fallback has to be
    # registered after the real commands or it would shadow them.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    app.add_handler(MessageHandler(filters.COMMAND, _on_unknown_command))

    logging.info("bot polling, authorized chat %s", config.TELEGRAM_CHAT_ID)

    # Pending updates are deliberately NOT dropped. This machine sleeps, and a
    # session logged at 9 PM with the lid shut sits queued on Telegram's side
    # until polling resumes. Those are precisely the entries worth having, and
    # discarding them would defeat the point of storing raw text at all.
    # Duplicates are not the risk they look like: Telegram advances the update
    # offset only once an update is acknowledged, so a normal restart does not
    # replay anything already handled.
    app.run_polling(allowed_updates=[Update.MESSAGE])


if __name__ == "__main__":
    # Run directly to test delivery alone:  python -m src.notify
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")
    send(f"athlete15 test message\nsent {now}")
    print(f"sent to chat {config.TELEGRAM_CHAT_ID}")
