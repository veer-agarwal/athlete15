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

import asyncio
import html
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

from src import brief, config, router, training

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


# Every chunk is wrapped in its own <pre>...</pre>, so the split limit must
# leave room for the tags. Opening <pre> once for the whole message and letting
# chunks share it does not work: Telegram parses each message independently and
# rejects an unclosed tag with a 400.
_PRE_OVERHEAD = len("<pre></pre>")


def send(text: str) -> None:
    """Send a message to the configured chat, rendered monospace.

    parse_mode HTML with the body wrapped in <pre>: the briefing's fixed
    template relies on column alignment, and Telegram only guarantees a
    monospace face inside pre blocks. HTML rather than MarkdownV2 because HTML
    needs only &, < and > escaped, while MarkdownV2 reserves - . ( ) ! and
    would reject lines like "high 84, 20% precip" wholesale.

    Escaping happens BEFORE chunking so the length budget is measured on the
    text actually sent; escaping per chunk could push a chunk back over the
    limit. The hard-slice branch of _split() could in principle cut through an
    escaped entity, but only on a single line over ~4000 characters, which
    nothing here produces.

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

    # quote=False escapes exactly &, < and >, which is all Telegram HTML needs.
    # The default would also turn quotes into entities for no benefit.
    escaped = html.escape(text, quote=False)

    for chunk in _split(escaped, limit=MAX_MESSAGE_CHARS - _PRE_OVERHEAD):
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": f"<pre>{chunk}</pre>",
            "parse_mode": "HTML",
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
    """Any non-command text goes to the intent router.

    router.handle_message owns the whole pipeline now: it writes the raw text to
    log_entries before anything else, classifies, applies, and never raises. This
    handler only carries the message across and sends the reply back.

    asyncio.to_thread runs a blocking function in a worker thread so the
    seconds-long LLM and CalDAV calls inside the router do not stall the polling
    event loop. The old handler got away with running its millisecond SQLite
    writes directly on the loop; the router does real network work and cannot.
    """
    if not _is_authorized(update):
        return

    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None or not message.text:
        return

    reply = await asyncio.to_thread(router.handle_message, chat.id, message.text)

    # Guard the empty case: Telegram rejects a zero-length message with a 400,
    # and a bare CHAT turn can come back empty. Split the rest, since a chat or
    # error reply can in principle run past the 4096 ceiling like the briefing.
    for chunk in _split(reply or "(no reply)"):
        await message.reply_text(chunk)


async def _on_status(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Current training load and open injuries, on demand."""
    if not _is_authorized(update):
        return
    await update.effective_message.reply_text(training.format_training_block())


async def _on_brief(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Build and send the full briefing on demand.

    brief.build() hits every source and can take tens of seconds, so it runs via
    asyncio.to_thread (worker thread, keeps the polling loop responsive) just
    like the router call in _on_text.
    """
    if not _is_authorized(update):
        return
    message = update.effective_message
    if message is None:
        return
    text = await asyncio.to_thread(brief.build)
    # reply_text has the same 4096-char ceiling as sendMessage, and the full
    # briefing can exceed it. Reuse _split rather than let Telegram 400.
    for chunk in _split(text):
        await message.reply_text(chunk)


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
        "send it again without the slash to log it as plain text.\n"
        "the only commands are /brief and /status"
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

    app.add_handler(CommandHandler("brief", _on_brief))
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
