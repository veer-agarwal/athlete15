"""Entry point. Scheduler plus Telegram bot in one process.

Run with:  python -m src.main
Or once, immediately, for testing:  python -m src.main --now

Phase 2 behavior: APScheduler fires the morning job, which builds the briefing
and sends it to Telegram. The bot polling loop lands in phase 4.
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from src import brief, config, db, notify
from src.sources import whoop

# The machine wakes from S3 sleep to run this and the scheduled task fires on
# resume, often before the wireless adapter has associated and DNS is answering.
# A fixed sleep is cruder than polling for connectivity but has no failure mode
# of its own, and the briefing has no deadline finer than "sometime around 7".
WAKE_DELAY_SECONDS = 30


def _configure_logging() -> None:
    """Timestamped INFO lines on stdout.

    stdout specifically. logging defaults to stderr, and the scheduled task
    redirects only stdout to brief.log, so leaving the default would produce an
    empty log file and no way to see what failed while asleep.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def send_brief() -> tuple[str, str | None]:
    """Build the briefing and send it. This is the scheduled job.

    brief.build() degrades per source internally, so it does not raise for a dead
    API. Delivery can still fail (no network, bad token), and an exception escaping
    a job would otherwise be swallowed by APScheduler's default logging. Log it
    here so a failed 7:00 AM send leaves a visible trace instead of nothing.

    Returns the briefing text and the UTC timestamp it was sent at, or None for
    that timestamp if delivery failed. The text is worth storing either way.
    """
    logging.info("briefing run started")
    text = brief.build()
    try:
        notify.send(text)
        sent_at = datetime.now(timezone.utc).isoformat()
        logging.info("briefing sent, %d chars", len(text))
        return text, sent_at
    except Exception:
        logging.exception("briefing send failed")
        return text, None


def authorize_whoop() -> int:
    """One-time interactive WHOOP OAuth flow. Returns a process exit code.

    Nothing needs to be listening on the redirect URI. The browser will fail to
    load http://localhost:8080/callback after you approve, and that is fine: the
    authorization code is sitting in the address bar of the page that failed, and
    that whole URL is what gets pasted back here.
    """
    if not config.WHOOP_CLIENT_ID or not config.WHOOP_CLIENT_SECRET:
        print("set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET in .env first")
        return 1

    print("\nOpen this URL in a browser and approve access:\n")
    print(whoop.build_authorize_url())
    print("\nYou will land on a page that fails to load. That is expected.")
    print("Copy the full URL out of the address bar and paste it below.\n")

    redirect_url = input("Redirect URL: ").strip()
    if not redirect_url:
        print("nothing pasted, aborting")
        return 1

    whoop.exchange_code(redirect_url)
    print(f"\ntoken stored at {config.WHOOP_TOKEN_PATH}")
    print("pull history with: python -m src.sources.whoop backfill YYYY-MM-DD")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="athlete15")
    parser.add_argument(
        "--now",
        action="store_true",
        help="run the briefing job once immediately and exit",
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help="run the one-time WHOOP browser authorization and store the token",
    )
    args = parser.parse_args()

    _configure_logging()

    # Before init_db: authorizing touches no local data, and the scheduler and
    # database messages would only interleave with the paste prompt.
    if args.auth:
        raise SystemExit(authorize_whoop())

    db.init_db()
    logging.info("database ready at %s", config.DB_PATH)

    if args.now:
        logging.info("manual run (--now)")
        logging.info("waiting %ds for the network to come up", WAKE_DELAY_SECONDS)
        time.sleep(WAKE_DELAY_SECONDS)

        text, sent_at = send_brief()

        # Local date, not UTC: this is the briefing *for* Thursday morning, and
        # after 8 PM Eastern a UTC date would file it under the next day.
        today = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")
        db.record_brief(today, text, sent_at)
        logging.info("brief recorded for %s (sent_at=%s)", today, sent_at)

        # Backup last, and never let a disconnected archive drive turn a delivered
        # briefing into a failed run.
        try:
            db.backup()
        except Exception:
            logging.exception("backup failed")

        return

    scheduler = BlockingScheduler(timezone=config.TIMEZONE)
    scheduler.add_job(
        send_brief,
        "cron",
        hour=config.BRIEF_HOUR,
        minute=config.BRIEF_MINUTE,
        # If the machine was asleep or the process was down at 7:00, fire late
        # rather than skip. An hour of grace covers a laptop lid opened at 8.
        misfire_grace_time=3600,
        # Never let two briefings overlap or stack up after a suspend.
        coalesce=True,
        max_instances=1,
        id="morning_brief",
    )

    logging.info(
        "scheduled morning briefing at %02d:%02d %s",
        config.BRIEF_HOUR,
        config.BRIEF_MINUTE,
        config.TIMEZONE,
    )

    # BlockingScheduler runs in this thread and does not return. Phase 4 moves the
    # bot polling loop in alongside it, at which point this becomes a
    # BackgroundScheduler and notify.run_bot() takes over the main thread.
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logging.info("shutting down")
        scheduler.shutdown()


if __name__ == "__main__":
    sys.exit(main())
