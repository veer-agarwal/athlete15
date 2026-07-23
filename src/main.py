"""Entry point. Scheduler plus Telegram bot in one process.

Run with:  python -m src.main

Phase 1 behavior: fetch weather and news, print to console, exit.
"""

from src.sources import weather


def run_once() -> None:
    """Phase 1: fetch and print. No database, no Telegram."""
    for item in weather.fetch():
        print(weather.format_line(item))

    # TODO phase 1: add news once you have written src/sources/news.py


def main() -> None:
    run_once()

    # TODO phase 2: replace with APScheduler + notify.run_bot()
    #   scheduler = BlockingScheduler(timezone=config.TIMEZONE)
    #   scheduler.add_job(send_brief, "cron",
    #                     hour=config.BRIEF_HOUR, minute=config.BRIEF_MINUTE)


if __name__ == "__main__":
    main()
