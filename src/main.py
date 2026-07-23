"""Entry point. Scheduler plus Telegram bot in one process.

Run with:  python -m src.main

Phase 1 behavior: fetch weather and news, print to console, exit.
"""

from src.sources import news, weather


def run_once() -> None:
    """Phase 1: fetch and print. No database, no Telegram.

    Each source is wrapped separately so one failure degrades one section
    instead of killing the briefing. The broad except is deliberate here: this
    is the degradation boundary, and anything a source raises should become a
    missing line rather than a crash. Inside the source modules, catches stay
    narrow.
    """
    try:
        for item in weather.fetch():
            print(weather.format_line(item))
    except Exception as exc:
        print(f"weather unavailable ({exc})")

    try:
        for line in news.format_lines(news.fetch()):
            print(line)
    except Exception as exc:
        print(f"news unavailable ({exc})")


def main() -> None:
    run_once()

    # TODO phase 2: replace with APScheduler + notify.run_bot()
    #   scheduler = BlockingScheduler(timezone=config.TIMEZONE)
    #   scheduler.add_job(send_brief, "cron",
    #                     hour=config.BRIEF_HOUR, minute=config.BRIEF_MINUTE)


if __name__ == "__main__":
    main()
