"""Entry point. Scheduler plus Telegram bot in one process.

Run with:  python -m src.main
Or once, immediately, for testing:  python -m src.main --now

Phase 2 behavior: APScheduler fires the morning job, which builds the briefing
and sends it to Telegram. The bot polling loop lands in phase 4.
"""

import argparse
import logging
import socket
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.blocking import BlockingScheduler

from src import brief, config, db, notify
from src.sources import whoop

# Hosts the briefing depends on. The wake probe waits until these answer before
# building, rather than sleeping a fixed interval and hoping the adapter is up.
PROBE_HOSTS = ("api.prod.whoop.com", "api.notion.com", "caldav.icloud.com")
NETWORK_PROBE_CAP_SECONDS = 180      # proceed anyway after this, partial beats nothing
NETWORK_PROBE_INTERVAL_SECONDS = 5   # recheck cadence while waiting
NETWORK_PROBE_TIMEOUT_SECONDS = 5    # per-host DNS+HEAD attempt

# Wake-triggered briefing (--wait-for-wake): poll WHOOP until last night's sleep
# cycle closes, then deliver. These bound that loop.
CUTOFF_HOUR = 11                     # local hour to give up waiting and send what we have
POLL_INTERVAL_SECONDS = 15 * 60      # gap between WHOOP checks while waiting
# A nap can close and score a short cycle. Counting it as the night's sleep would
# fire the briefing hours early on a 40-minute "night", so require real sleep.
NAP_MIN_SLEEP_HOURS = 3.0


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


def send_brief(note: str | None = None) -> tuple[str, str | None]:
    """Build the briefing and send it. This is the scheduled job.

    brief.build() degrades per source internally, so it does not raise for a dead
    API. Delivery can still fail (no network, bad token), and an exception escaping
    a job would otherwise be swallowed by APScheduler's default logging. Log it
    here so a failed 7:00 AM send leaves a visible trace instead of nothing.

    A note, when given, is prepended as its own line: the wake job uses it to say
    "no completed sleep cycle by 11:00" so a recovery-less briefing does not read
    as if the strap simply had nothing.

    Returns the briefing text and the UTC timestamp it was sent at, or None for
    that timestamp if delivery failed. The text is worth storing either way.
    """
    logging.info("briefing run started")
    text = brief.build()
    if note:
        text = f"{note}\n\n{text}"
    try:
        notify.send(text)
        sent_at = datetime.now(timezone.utc).isoformat()
        logging.info("briefing sent, %d chars", len(text))
        return text, sent_at
    except Exception:
        logging.exception("briefing send failed")
        return text, None


def _deliver(note: str | None = None) -> None:
    """Build, send, record, and back up. Shared by --now and --wait-for-wake.

    Recording uses the local date the briefing is FOR, so a same-morning re-run
    updates that day's row rather than adding a second. Backup runs last and a
    disconnected archive drive never turns a delivered briefing into a failed run.
    """
    text, sent_at = send_brief(note=note)

    # Local date, not UTC: this is the briefing for this morning, and after 8 PM
    # Eastern a UTC date would file it under the next day.
    today = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")
    db.record_brief(today, text, sent_at)
    logging.info("brief recorded for %s (sent_at=%s)", today, sent_at)

    try:
        db.backup()
    except Exception:
        logging.exception("backup failed")


def _host_reachable(host: str) -> bool:
    """DNS resolves AND an HTTPS HEAD gets any response back.

    Any HTTP status counts as reachable: a 401 or 405 from a HEAD still proves
    the connection is up, which is all the probe is checking. Only a DNS failure
    or a connection/timeout error means not-yet-connected.
    """
    try:
        socket.getaddrinfo(host, 443)
    except OSError:
        return False
    try:
        requests.head(f"https://{host}", timeout=NETWORK_PROBE_TIMEOUT_SECONDS)
    except requests.RequestException:
        return False
    return True


def _wait_for_network() -> float:
    """Probe every host until all reachable or the cap. Returns seconds waited.

    Replaces the old fixed 30s post-wake sleep. The wireless adapter is often not
    associated the instant the machine resumes from S3, and a flat sleep is either
    wasteful or too short. Proceeds anyway after the cap so a partial briefing
    still arrives rather than nothing.
    """
    start = time.monotonic()
    while True:
        unreachable = [host for host in PROBE_HOSTS if not _host_reachable(host)]
        elapsed = time.monotonic() - start

        if not unreachable:
            logging.info("network up after %.0fs, all probe hosts reachable", elapsed)
            return elapsed
        if elapsed >= NETWORK_PROBE_CAP_SECONDS:
            logging.warning(
                "network probe cap %ds reached, still unreachable: %s. proceeding anyway",
                NETWORK_PROBE_CAP_SECONDS, ", ".join(unreachable),
            )
            return elapsed

        logging.info("waiting for network, unreachable: %s", ", ".join(unreachable))
        time.sleep(NETWORK_PROBE_INTERVAL_SECONDS)


def _is_main_sleep(metrics: dict) -> bool:
    """Whether a fetched WHOOP day is last night's main sleep, not a nap."""
    hours = metrics.get("sleep_hours")
    return hours is not None and hours > NAP_MIN_SLEEP_HOURS


def wait_for_wake() -> None:
    """Poll WHOOP after an S3 wake until last night's sleep is scored, then deliver.

    Polls whoop.fetch_current(), the cycle in progress, NOT whoop.fetch(). The
    most recently completed cycle closed when you went to bed and is scored while
    you are still asleep, so waiting on it fired the briefing in the middle of the
    night with the previous night's numbers. The open cycle gets a scored recovery
    and sleep only once you wake and the strap syncs, which is the event this is
    actually waiting for.

    Idempotent: if a briefing is already recorded for today, exits without
    sending a second one. Distinguishes "not scored yet" (retry) from "WHOOP
    request failed" (network) from "WHOOP auth failed" (fatal, stop polling) so a
    slow strap sync is never misread as an outage. At the 11:00 local cutoff it
    sends whatever the other sources have, with a note saying so.
    """
    tz = ZoneInfo(config.TIMEZONE)
    today = datetime.now(tz).strftime("%Y-%m-%d")

    if db.brief_exists(today):
        logging.info("brief already recorded for %s, exiting (idempotent)", today)
        return

    _wait_for_network()

    cutoff = datetime.now(tz).replace(
        hour=CUTOFF_HOUR, minute=0, second=0, microsecond=0
    )

    while True:
        # Re-checked every iteration, not just at entry: if the fixed 7:00 --now
        # job (or a manual run) delivered while this loop was in its 15-minute
        # sleep, that morning is already done and a second Telegram message would
        # be a duplicate. record_brief upserts on date, so the DB stays single-row
        # regardless; this guards the phone.
        if db.brief_exists(today):
            logging.info("brief for %s already recorded elsewhere, exiting", today)
            return

        try:
            items = whoop.fetch_current()
        except whoop.WhoopAuthError:
            # The stored token is dead; WHOOP cannot succeed this run no matter how
            # long we wait. fetch_current already logged the --auth remediation line.
            logging.error("WHOOP auth failed during wake wait, delivering without recovery")
            _deliver(note="WHOOP auth failed, recovery unavailable. Run: python -m src.main --auth")
            return
        except requests.RequestException as exc:
            # A network fault, NOT "no cycle yet". Logged distinctly because
            # conflating the two is what turned a real auth outage into a
            # misdiagnosed timing problem before.
            logging.warning("WHOOP request failed (will retry): %s", exc)
        else:
            if items and _is_main_sleep(items[0]):
                logging.info("last night's sleep is scored, delivering briefing")
                _deliver()
                return
            logging.info("whoop: last night's main sleep is not scored yet")

        if datetime.now(tz) >= cutoff:
            logging.info(
                "reached %02d:00 cutoff with no scored sleep, sending what is available",
                CUTOFF_HOUR,
            )
            _deliver(
                note=f"No scored sleep from last night by {CUTOFF_HOUR}:00, sending without recovery."
            )
            return

        # Sleep until the next check, but never past the cutoff.
        remaining = (cutoff - datetime.now(tz)).total_seconds()
        nap = min(POLL_INTERVAL_SECONDS, max(0.0, remaining))
        logging.info("sleeping %.0f min before next WHOOP check", nap / 60)
        time.sleep(nap)


# What the briefing takes from each cycle, keyed by whoop.audit()'s briefing_role.
# Printed per cycle so the current/completed split is verifiable from the table
# alone, without sending a message and reading it on the phone.
_BRIEFING_ROLE_LINES = {
    "current": "briefing: recovery, sleep, HRV, RHR -> header line, sleep note",
    "completed": "briefing: strain -> Yesterday's Strain, workouts + date -> TRAINING",
    None: "briefing: not used",
}


def whoop_audit() -> int:
    """Print a 7-day WHOOP cycle/workout audit for diffing against the app.

    Read-only. Shows each cycle's local start and end, day strain, recovery, and
    sleep next to the local date the code assigned it, plus the workouts bucketed
    onto that date, so date bucketing can be verified by eye against the app.

    Each cycle also gets a line naming the briefing fields sourced from it. Sleep
    and recovery come from the cycle in progress while strain and TRAINING come
    from the last completed one, and a table that only showed correct numbers
    under correct dates could not have caught them being read off the wrong cycle.
    """
    try:
        rows = whoop.audit(days=7)
    except Exception as exc:
        print(f"WHOOP audit failed: {exc}")
        return 1

    if not rows:
        print("no WHOOP cycles in the last 7 days")
        return 0

    def num(value: float | int | None, spec: str) -> str:
        return format(value, spec) if value is not None else "-"

    # Count the rows actually printed rather than restating the requested window:
    # a 7 day lookback spans 8 calendar days inclusive and can return 9 cycles once
    # the in-progress one is included, so "last 7 days" read as a miscount.
    print(f"WHOOP audit, {len(rows)} cycles (local time). Diff against the WHOOP app.\n")
    for row in rows:
        print(f"{row['date']}  cycle {row['cycle_id']}")
        print(f"    start {row['start_local']}    end {row['end_local']}")
        print(
            f"    strain {num(row['strain'], '.1f')}"
            f"    recovery {num(row['recovery_score'], '.0f')}"
            f"    sleep {num(row['sleep_hours'], '.1f')}h"
            f"    sleep perf {num(row['sleep_performance'], '.0f')}%"
        )
        if row["workouts"]:
            for workout in row["workouts"]:
                dur = f"{workout['duration_min']}min" if workout["duration_min"] is not None else "-"
                print(
                    f"      {workout['sport']:<16}{dur:>8}    "
                    f"strain {num(workout['strain'], '.1f')}"
                )
        else:
            print("      (no workouts)")
        # Both lookups defensive: a row without the key, or with a role this table
        # does not know, prints "not used" instead of taking the table down.
        role_line = _BRIEFING_ROLE_LINES.get(
            row.get("briefing_role"), _BRIEFING_ROLE_LINES[None]
        )
        print(f"    {role_line}")
        print()
    return 0


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
    parser.add_argument(
        "--bot",
        action="store_true",
        help="start Telegram polling for log entries and block",
    )
    parser.add_argument(
        "--wait-for-wake",
        action="store_true",
        help="poll WHOOP until the sleep cycle closes, then deliver (11:00 cutoff)",
    )
    parser.add_argument(
        "--whoop-audit",
        action="store_true",
        help="print a 7-day WHOOP cycle/workout table for date-bucketing checks",
    )
    args = parser.parse_args()

    _configure_logging()

    # Before init_db: authorizing touches no local data, and the scheduler and
    # database messages would only interleave with the paste prompt.
    if args.auth:
        raise SystemExit(authorize_whoop())

    # Read-only diagnostic, no database needed. Kept before init_db so its table
    # is not buried under startup log lines.
    if args.whoop_audit:
        raise SystemExit(whoop_audit())

    db.init_db()
    logging.info("database ready at %s", config.DB_PATH)

    if args.bot:
        # Polling only, no scheduler. The eventual shape is both in one process
        # (BackgroundScheduler alongside the bot, per the note at the bottom of
        # this file), but PTB owns the main thread's event loop and merging the
        # two is its own change. Run --bot and the scheduled --now task side by
        # side until then.
        notify.run_bot()
        return

    if args.wait_for_wake:
        logging.info("wake-triggered run (--wait-for-wake)")
        wait_for_wake()
        return

    if args.now:
        logging.info("manual run (--now)")
        # Was a fixed 30s sleep; now waits on real connectivity after S3 wake.
        _wait_for_network()
        _deliver()
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
