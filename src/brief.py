"""Assembles the morning briefing.

Fixed template. Python renders every section deterministically from stored data;
the local model contributes exactly one line (the sleep sentence, see llm.py).
Numbers, dates, and layout never pass through the model, so it cannot mangle
them.

Target layout, each block separated by a blank line and omitted entirely when it
has no data:

    Recovery 54  |  Sleep 6h12m (71%)  |  HRV 62  |  RHR 51

    <one model sentence on last night vs baseline>

    TODAY
      3:00p  Lift - facility

    DUE
      Jul 25  Econ pset 3

    TRAINING
      Yesterday: court 90min RPE 6
      7d load 1840, 28d avg 1610
      Right shoulder, 3/10, day 12

    72F, high 84, partly cloudy, 20% precip

    NEWS
      - headline one

Delivery wraps this in <pre> (see notify.send), so the two space indents and
column spacing survive as real monospace alignment on the phone.

RESILIENCE: wrap every source call. One dead API must degrade to a missing
section, not kill the whole message. The log says what was omitted and why.

The briefing REPORTS. It does not coach. No training recommendations.
"""

import json
import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from src import config, db, llm, training
from src.sources import calendar, weather, news, notion, whoop

# Retry policy for source fetches. The failure being handled is a network adapter
# that has not associated yet after an S3 wake. The wake job now runs a network
# probe before building, so by the time these fetches run the adapter is usually
# up; this is the backstop for the case where a single host is still settling.
# 15s then 30s matches the WHOOP and Notion client backoffs and gives a cold
# connection real time rather than hammering it twice in 20 seconds.
FETCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (15, 30)

# Cap on DUE lines so the section cannot eat the message. Six covers a heavy
# week; past that the count line says what was cut.
MAX_DUE_ITEMS = 6

# The sleep note compares last night to this many trailing days.
BASELINE_DAYS = 14


def build() -> str:
    """Assemble the full briefing text.

    Every block renders independently and returns "" when it has no data or its
    source failed, and empty blocks are dropped rather than printed as bare
    headers. Block order is fixed here and nowhere else.
    """
    blocks: list[str] = []

    # WHOOP is fetched once and feeds three things from TWO different cycles.
    # The CURRENT (open) cycle began when I woke this morning, so it holds last
    # night's sleep and today's recovery: that is the header and the sleep note.
    # The COMPLETED cycle is yesterday, so its day strain is final: that is the
    # strain line and the TRAINING date. Using one cycle for all of it is what
    # made the header a night stale.
    current, completed = _fetch_metrics()
    _log_cycle_sources(current, completed)

    if current is not None or completed is not None:
        blocks.append(_guard("metrics line", lambda: _metrics_line(current, completed)))
    if current is not None:
        blocks.append(_guard("sleep note", lambda: _sleep_note(current)))

    # Persist WHOOP workouts before the TRAINING block reads them below. Best
    # effort and self-contained: it renders nothing itself and never raises.
    _store_yesterday_workouts()

    # "Yesterday" for TRAINING is the COMPLETED cycle's assigned date, taken from
    # the WHOOP metrics rather than calendar arithmetic. None when WHOOP is down,
    # in which case _training_block falls back to calendar yesterday.
    cycle_date = completed["date"] if completed is not None else None

    blocks.append(_guard("TODAY", _today_block))
    blocks.append(_guard("DUE", _due_block))
    blocks.append(_guard("TRAINING", lambda: _training_block(cycle_date)))
    blocks.append(_guard("weather", _weather_block))
    blocks.append(_guard("NEWS", _news_block))

    kept = [block for block in blocks if block]
    if not kept:
        # Telegram rejects an empty message, and a silent no-send looks like a
        # dead scheduled task. Say explicitly that the pipeline ran dry.
        logging.error("briefing rendered no sections at all")
        return "no data available this morning"

    return "\n\n".join(kept)


def _guard(name: str, render: Callable[[], str]) -> str:
    """Run one block renderer, degrading any failure to an omitted section.

    The blocks each catch their own fetch failures for specific logging, but
    their render loops also parse dates and timestamps that came from a source,
    and a malformed field there would otherwise escape build() entirely.
    main.send_brief calls build() unguarded on the promise that it does not
    raise, so this is the boundary that keeps one bad row from costing the
    whole 7 AM message.
    """
    try:
        return render()
    except Exception:
        logging.exception("%s block failed, omitted", name)
        return ""


# ----------------------------------------------------------------------------------
# fetching


def _fetch_with_retry(
    name: str,
    fetch: Callable[[], list[dict]],
    retry_on: type[Exception] | tuple[type[Exception], ...],
) -> list[dict]:
    """Call fetch(), retrying transient failures, and log the outcome.

    Re-raises the last exception once attempts are exhausted, leaving the caller
    to decide how the section degrades. Anything not in retry_on propagates
    immediately: a KeyError from our own parsing will not be sitting out the
    retry backoffs before it surfaces.
    """
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            items = fetch()
        except retry_on as exc:
            if attempt == FETCH_ATTEMPTS:
                logging.error(
                    "%s failed after %d attempts: %s", name, FETCH_ATTEMPTS, exc
                )
                raise
            # Clamp so a shorter schedule than the attempt count reuses its last
            # value rather than running off the end.
            wait = RETRY_BACKOFF_SECONDS[
                min(attempt, len(RETRY_BACKOFF_SECONDS)) - 1
            ]
            logging.warning(
                "%s attempt %d/%d failed: %s, retrying in %ds",
                name, attempt, FETCH_ATTEMPTS, exc, wait,
            )
            time.sleep(wait)
        else:
            logging.info("%s ok on attempt %d, %d item(s)", name, attempt, len(items))
            return items

    raise AssertionError("unreachable")  # the loop always returns or raises


def _fetch_metrics() -> tuple[dict | None, dict | None]:
    """(current, completed) WHOOP cycles, stored to daily_metrics on the way.

    whoop.fetch() returns up to two rows tagged by cycle_kind; this splits them
    apart by tag rather than by position, so a run where only one of the two
    exists cannot silently hand the wrong cycle to the header. Either element is
    None when that cycle is not available yet.

    This is the only fetch that persists what it got. WHOOP is the source the
    briefing trends over, and the round trip has already been paid for here. The
    write cannot take the section down with it: the numbers are already in hand,
    and a locked database is not a reason to drop them from the message.

    Both rows are written. They land on different dates (current is today,
    completed is yesterday), and the current row's strain is the day's partial
    total, which tomorrow's completed row overwrites with the final number:
    upsert_daily_metrics COALESCEs a new non-null value over the stored one.
    """
    try:
        # RuntimeError, which is what fetch() raises for a missing token, is
        # deliberately not in the retry set. An absent whoop_token.json does not
        # resolve itself in ten seconds.
        items = _fetch_with_retry("whoop", whoop.fetch, requests.RequestException)
    except Exception as exc:
        logging.warning("whoop unavailable, metrics line omitted: %s", exc)
        return None, None

    by_kind = {item.get("cycle_kind"): item for item in items}
    current, completed = by_kind.get("current"), by_kind.get("completed")

    if current is None and completed is None:
        # Expected before the strap syncs after you wake, not a failure.
        logging.info("whoop: no scored cycle yet, metrics line omitted")
        return None, None

    for kind, row in (("current", current), ("completed", completed)):
        if row is None:
            logging.info("whoop: no %s cycle in this fetch", kind)
            continue
        try:
            # db_row strips cycle_kind and cycle_id, which are not columns.
            db.upsert_daily_metrics(whoop.db_row(row))
            logging.info(
                "whoop %s cycle %s stored for %s",
                kind, row.get("cycle_id"), row["date"],
            )
        except Exception:
            logging.exception(
                "storing whoop %s metrics failed, briefing continues", kind
            )

    return current, completed


def _log_cycle_sources(current: dict | None, completed: dict | None) -> None:
    """Record which WHOOP cycle each briefing field was taken from.

    The whole point of the two-cycle split is that the header and the strain
    line come from different cycles. When a number in the message looks wrong,
    this line in brief.log says which cycle id to open in the WHOOP app, which
    is the difference between checking it in a minute and re-deriving the
    bucketing by hand.
    """

    def where(row: dict | None) -> str:
        if row is None:
            return "unavailable"
        return f"cycle {row.get('cycle_id')} ({row.get('date')})"

    logging.info(
        "brief: recovery/sleep/HRV/RHR <- %s; yesterday's strain and TRAINING <- %s",
        where(current),
        where(completed),
    )


def _store_yesterday_workouts() -> None:
    """Fetch and persist recent WHOOP workouts so TRAINING can show them.

    fetch_workouts defaults to a trailing seven-day window, which covers
    yesterday and lets a workout WHOOP scored late still land. Kept separate
    from _fetch_metrics because workouts come from a different endpoint: one
    failing must not cost the other. Best effort, never raises into build().

    A WhoopAuthError (dead token) is not in the retry set, so it surfaces here
    on the first attempt and is logged once rather than retried three times.
    """
    try:
        workouts = _fetch_with_retry(
            "whoop workouts", whoop.fetch_workouts, requests.RequestException
        )
    except Exception as exc:
        logging.warning("whoop workouts unavailable, TRAINING may omit them: %s", exc)
        return

    try:
        whoop.store_workouts(workouts)
    except Exception:
        logging.exception("storing whoop workouts failed, briefing continues")


# ----------------------------------------------------------------------------------
# blocks, in layout order


def _metrics_line(current: dict | None, completed: dict | None) -> str:
    """The recovery header, plus a second strain line:

        Recovery 54  |  Sleep 6h12m (71%)  |  HRV 62  |  RHR 51
        Yesterday's Strain 14.2

    The two lines come from two different WHOOP cycles and that is the point.
    Recovery, sleep, HRV and RHR are last night's, which live on the CURRENT
    (still open) cycle. Strain is yesterday's finished total, which lives on the
    COMPLETED cycle. Reading both off one cycle is what made the header report
    the morning before.

    Fields WHOOP did not score are dropped rather than printed as a dash, so a
    partial day stays readable. Explicit :.0f throughout because WHOOP sends
    whole numbers as floats and would otherwise print "Recovery 54.0".

    Day strain is the cycle score (completed['strain'], set from
    cycle.score.strain in whoop._row), NOT a sum of the day's workout strains.
    Those are different numbers and the cycle score is the one the WHOOP app
    displays. It sits on its own line because it summarizes yesterday's whole
    day, not last night.
    """
    current = current or {}
    completed = completed or {}

    parts = []
    if current.get("recovery_score") is not None:
        parts.append(f"Recovery {current['recovery_score']:.0f}")
    if current.get("sleep_hours") is not None:
        sleep = f"Sleep {_fmt_duration(current['sleep_hours'])}"
        if current.get("sleep_performance") is not None:
            sleep += f" ({current['sleep_performance']:.0f}%)"
        parts.append(sleep)
    if current.get("hrv_ms") is not None:
        parts.append(f"HRV {current['hrv_ms']:.0f}")
    if current.get("resting_hr") is not None:
        parts.append(f"RHR {current['resting_hr']:.0f}")

    lines = ["  |  ".join(parts)] if parts else []
    if completed.get("strain") is not None:
        lines.append(f"Yesterday's Strain {completed['strain']:.1f}")
    return "\n".join(lines)


def _sleep_note(metrics: dict) -> str:
    """The model's one sentence, or "" whenever anything is not right.

    Omission is the designed failure mode at every step here: no baseline yet,
    no current values, Ollama down, or a rambling generation all mean the line
    simply is not there. No filler stand-in, per the template rules.
    """
    sleep = metrics.get("sleep_hours")
    recovery = metrics.get("recovery_score")
    if sleep is None or recovery is None:
        logging.info("sleep note omitted: last night not fully scored")
        return ""

    try:
        # Baseline ends the day BEFORE this metric's date, so last night is
        # compared against history rather than being averaged into it.
        end = date.fromisoformat(metrics["date"]) - timedelta(days=1)
        start = end - timedelta(days=BASELINE_DAYS - 1)
        baseline = db.average_metrics(start.isoformat(), end.isoformat())
    except Exception:
        logging.exception("baseline query failed, sleep note omitted")
        return ""

    if baseline["sleep_hours"] is None or baseline["recovery_score"] is None:
        logging.info("sleep note omitted: no baseline history yet")
        return ""

    note = llm.generate_sleep_note(
        sleep_hours=float(sleep),
        recovery=float(recovery),
        avg_sleep_hours=float(baseline["sleep_hours"]),
        avg_recovery=float(baseline["recovery_score"]),
    )
    # generate_sleep_note already logged the reason when it returned None.
    return note or ""


def _today_block() -> str:
    """TODAY section from the iCloud calendar over CalDAV.

    Alone among the sections, failure here degrades to a visible line instead
    of omission: a day with no events and a day where the calendar could not
    be reached must not look identical, or a dead calendar reads as a free
    morning. No events at all still omits the section like everywhere else.

    Retries on calendar.TRANSIENT_ERRORS, which the source module exports so
    caldav's exception types stay out of this file. The credential
    RuntimeError from a 401 is deliberately not transient: a revoked app
    password does not fix itself in ten seconds.
    """
    try:
        events = _fetch_with_retry(
            "calendar", calendar.fetch, calendar.TRANSIENT_ERRORS
        )
    except Exception as exc:
        logging.warning("calendar unavailable, TODAY degraded: %s", exc)
        return "TODAY\n  calendar unavailable"

    if not events:
        return ""

    tz = ZoneInfo(config.TIMEZONE)
    lines = ["TODAY"]
    for event in events:
        title = event["summary"]
        if event.get("location"):
            title += f" - {event['location']}"
        if event["all_day"]:
            # Same width as the DUE block's "no date" label; fetch() sorts
            # all-day events first so the timeless rows sit together on top.
            lines.append(f"  all day  {title}")
        else:
            start = datetime.fromisoformat(event["start_utc"].replace("Z", "+00:00"))
            lines.append(f"  {_fmt_time(start.astimezone(tz))}  {title}")
    return "\n".join(lines)


def _due_block() -> str:
    """DUE section from Notion, capped at MAX_DUE_ITEMS.

    Called without _fetch_with_retry, alone among the sections: notion._api
    already retries internally, because only it can see the Retry-After header a
    429 carries. Stacking the generic retry on top would make nine attempts.
    """
    try:
        items = notion.fetch()
    except Exception as exc:
        logging.warning("notion unavailable, DUE omitted: %s", exc)
        return ""

    # Persist after fetching, same pattern as WHOOP: a database problem must not
    # cost the message data it already has.
    try:
        for item in items:
            db.upsert_task(item)
        logging.info("stored %d notion task(s)", len(items))
    except Exception:
        logging.exception("storing notion tasks failed, briefing continues")

    if not items:
        return ""

    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    lines = ["DUE"]
    for item in items[:MAX_DUE_ITEMS]:
        title = item["title"] or "(untitled)"
        due = item["due_date"]
        if due is None:
            # One character wider than a date, slightly breaking the column.
            # Undated tasks are rare and sorted last by notion.fetch(), so the
            # misalignment sits at the bottom of the block.
            lines.append(f"  no date  {title}")
        else:
            due_day = date.fromisoformat(due[:10])
            # The suffix, not the date column, carries urgency: OVERDUE in the
            # date column would break the fixed width alignment.
            suffix = " (overdue)" if due_day < today else ""
            lines.append(f"  {_fmt_date(due_day)}  {title}{suffix}")

    omitted = len(items) - MAX_DUE_ITEMS
    if omitted > 0:
        lines.append(f"  +{omitted} more")
    return "\n".join(lines)


def _training_block(cycle_date: str | None = None) -> str:
    """TRAINING section: yesterday, load, active injuries. All local data.

    "Yesterday" is the most recently COMPLETED WHOOP cycle, whose assigned local
    date (cycle_date) is the local date that cycle STARTED, via whoop._cycle_date,
    NOT calendar arithmetic. This matters because the job runs at 11:00 UTC, which
    is still the previous UTC day for the first hours of the morning, so naive UTC
    date math would be off by one; and because a missed strap sync means the last
    completed cycle is not simply "today minus one". Calendar yesterday is only the
    fallback for when WHOOP is unavailable and no cycle date was passed.

    Rendered here rather than reusing training.format_training_block(), which
    keeps its own looser format for the /status bot command. Load is stated as
    the two numbers only, no ratio and no verdict, per the template and the
    hard rule that the briefing reports rather than judges.
    """
    try:
        today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
        target = cycle_date or (today - timedelta(days=1)).isoformat()
        logged = training.sessions_between(target, target)
        workouts = db.whoop_workouts_between(target, target)
        load = training.acute_chronic_ratio()
        injuries = training.active_injuries()
    except Exception:
        logging.exception("training queries failed, TRAINING omitted")
        return ""

    # "No data" for this section: nothing on the target date (neither hand-logged
    # nor WHOOP-measured), no load in 28 days, and no open injuries. A session
    # logged without RPE inside the window contributes zero load and can slip past
    # this test; that is acceptable for a section whose job is current status, and
    # /status still shows everything.
    if (
        not logged
        and not workouts
        and not injuries
        and not load["acute"]
        and not load["chronic"]
    ):
        return ""

    lines = ["TRAINING", _yesterday_line(workouts, logged)]
    lines.append(f"  7d load {load['acute']:.0f}, 28d avg {load['chronic']:.0f}")

    for injury in injuries:
        name = injury["body_part"]
        if injury.get("side"):
            name = f"{injury['side']} {name}"
        pain = (
            f"{injury['latest_pain']}/10"
            if injury["latest_pain"] is not None
            else "no pain logged"
        )
        days = (today - date.fromisoformat(injury["onset_date"])).days + 1
        lines.append(f"  {name.capitalize()}, {pain}, day {days}")

    return "\n".join(lines)


def _weather_block() -> str:
    """72F, high 84, partly cloudy, 20% precip

    No header and no low temperature, per the template. The full forecast dict
    is still fetched and available if the line grows fields later.
    """
    try:
        items = _fetch_with_retry("weather", weather.fetch, requests.RequestException)
    except Exception as exc:
        logging.warning("weather unavailable, line omitted: %s", exc)
        return ""
    if not items:
        return ""

    w = items[0]
    return (
        f"{w['temp_now_f']}F, high {w['high_f']}, "
        f"{w['condition']}, {w['precip_chance']}% precip"
    )


def _news_block() -> str:
    """NEWS section, headline titles only."""
    try:
        # RuntimeError is in the retry set on purpose. news.fetch() catches
        # RequestException per feed itself and raises RuntimeError("every news
        # feed failed") when none survive, so a network that is not up yet
        # surfaces here as RuntimeError and never as a requests exception.
        items = _fetch_with_retry(
            "news", news.fetch, (requests.RequestException, RuntimeError)
        )
    except Exception as exc:
        logging.warning("news unavailable, NEWS omitted: %s", exc)
        return ""
    if not items:
        return ""

    return "\n".join(["NEWS"] + [f"  - {item['title']}" for item in items])


# ----------------------------------------------------------------------------------
# formatting primitives


def _fmt_duration(hours: float) -> str:
    """6.2 hours -> '6h12m'. Minutes zero padded so columns hold width."""
    total_min = round(hours * 60)
    return f"{total_min // 60}h{total_min % 60:02d}m"


def _fmt_time(moment: datetime) -> str:
    """A local datetime -> '3:00p'. No leading zero, single letter am/pm."""
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d}{'a' if moment.hour < 12 else 'p'}"


def _fmt_date(day: date) -> str:
    """A date -> 'Jul 25'. Day padded to width 2 ('Jul  5') so the DUE date
    column stays fixed width in monospace."""
    return f"{day.strftime('%b')} {day.day:>2}"


def _session_desc(session: dict) -> str:
    """One logged session -> 'court 90min RPE 6', dropping missing fields."""
    parts = [session["type"]]
    if session.get("duration_min") is not None:
        parts.append(f"{session['duration_min']}min")
    if session.get("rpe") is not None:
        parts.append(f"RPE {session['rpe']}")
    return " ".join(parts)


def _yesterday_line(workouts: list[dict], sessions: list[dict]) -> str:
    """The 'Yesterday:' line for TRAINING.

    Leads with the single LONGEST WHOOP workout of the completed cycle, with a
    count of the rest ("(+2 other activities)") rather than dropping them
    silently, and pairs it with the RPE logged for that date. Two different
    measurements of the day on one line: WHOOP's objective sport/duration/strain
    and the subjective RPE. RPE shows as a number when one was logged, else
    "RPE not logged", because strain systematically undervalues resistance work
    and a session with no RPE is a gap worth naming rather than hiding.

    Falls back to the hand-logged session line when WHOOP recorded nothing, and
    to "nothing logged" when neither source has anything.
    """
    date_rpe = _date_rpe(sessions)

    if workouts:
        longest = max(workouts, key=lambda w: w.get("duration_min") or 0)
        others = len(workouts) - 1

        strain_part = (
            f", strain {longest['strain']:.1f}"
            if longest.get("strain") is not None
            else ""
        )
        rpe_part = f"RPE {date_rpe:g}" if date_rpe is not None else "RPE not logged"
        if others > 0:
            noun = "activity" if others == 1 else "activities"
            others_part = f" (+{others} other {noun})"
        else:
            others_part = ""

        return (
            f"  Yesterday: {_workout_sport(longest)} "
            f"{_fmt_workout_dur(longest.get('duration_min'))}"
            f"{strain_part}, {rpe_part}{others_part}"
        )

    if sessions:
        # No WHOOP workout, but something was typed in. One line even for
        # two-a-days; repeated "Yesterday:" labels read as a bug.
        return "  Yesterday: " + "; ".join(_session_desc(s) for s in sessions)

    return "  Yesterday: nothing logged"


def _date_rpe(sessions: list[dict]) -> int | float | None:
    """The RPE logged for the date, or None. Max when several sessions carry one.

    A two-a-day produces two sessions; the higher RPE is the more conservative
    read of how hard the day was, and it is a single deterministic value to show.
    """
    rpes = [s["rpe"] for s in sessions if s.get("rpe") is not None]
    return max(rpes) if rpes else None


def _workout_sport(workout: dict) -> str:
    """WHOOP's own label for a workout, capitalized, e.g. 'Volleyball'.

    Prefers the stored sport_name column, falls back to the raw payload (rows
    stored before that column existed keep it in raw_json), then to the sport id,
    so a workout is never labeled blank.
    """
    name = workout.get("sport_name")
    if not name:
        raw = workout.get("raw_json")
        if raw:
            try:
                name = json.loads(raw).get("sport_name")
            except (ValueError, TypeError):
                name = None
    if not name:
        sport_id = workout.get("sport_id")
        return f"Sport {sport_id}" if sport_id is not None else "Workout"
    return name.capitalize()


def _fmt_workout_dur(minutes: int | None) -> str:
    """WHOOP workout minutes -> '2h05m', or '54m' under an hour, '?' if missing."""
    if minutes is None:
        return "?"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"
