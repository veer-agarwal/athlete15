"""Assembles the morning briefing. Phase 2 onward.

Order of sections, per the target output:
    1. Recovery and sleep numbers
    2. Short narrative on last night's sleep
    3. Today's calendar
    4. Homework due
    5. Training status: yesterday's session, load trend, active injuries
    6. Weather, then news

RESILIENCE: wrap every source call. One dead API must degrade to a single line
saying so, not kill the whole message. You will notice a missing WHOOP line. You
will not notice a briefing that silently never arrived.

The briefing REPORTS. It does not coach. No training recommendations.
"""

import logging
import time
from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from src import config, db
from src.sources import weather, news, notion, whoop

# Retry policy for source fetches. The failure being handled is a network adapter
# that has not associated yet after an S3 wake, so backoff is fixed rather than
# exponential: the condition clears on a wall clock timescale of seconds, and
# there is no server being overloaded to back off from.
FETCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 10


def build() -> str:
    """Assemble the full briefing text.

    Recovery, weather and news, plain template, no LLM. Later phases add sections
    and hand the assembled facts to llm.py for phrasing.
    """
    # Header stamped in local time. Everything stored is UTC per the conventions;
    # this is display, so it converts here.
    today = datetime.now(ZoneInfo(config.TIMEZONE))
    lines = [f"athlete15 briefing, {today.strftime('%a %b %d')}", ""]

    lines.append("Recovery")
    lines.extend(_whoop_lines())
    lines.append("")

    # Position 4 in the target output, after calendar and before training.
    # Calendar and training sections do not exist in the briefing yet, so today
    # it sits directly after recovery; when those arrive they go around it.
    lines.append("Coursework")
    lines.extend(_coursework_lines())
    lines.append("")

    lines.append("Weather")
    lines.extend(_weather_lines())
    lines.append("")

    lines.append("News")
    lines.extend(_news_lines())

    return "\n".join(lines)


def _fetch_with_retry(
    name: str,
    fetch: Callable[[], list[dict]],
    retry_on: type[Exception] | tuple[type[Exception], ...],
) -> list[dict]:
    """Call fetch(), retrying transient failures, and log the outcome.

    Re-raises the last exception once attempts are exhausted, leaving the caller
    to decide how the section degrades. Anything not in retry_on propagates
    immediately: a KeyError from our own parsing will not be sitting out three
    ten second sleeps before it surfaces.
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
            logging.warning(
                "%s attempt %d/%d failed: %s, retrying in %ds",
                name, attempt, FETCH_ATTEMPTS, exc, RETRY_BACKOFF_SECONDS,
            )
            time.sleep(RETRY_BACKOFF_SECONDS)
        else:
            logging.info("%s ok on attempt %d, %d item(s)", name, attempt, len(items))
            return items

    raise AssertionError("unreachable")  # the loop always returns or raises


def _whoop_lines() -> list[str]:
    """Recovery section body, and the day's metrics into daily_metrics.

    This is the only section that persists what it fetched. WHOOP is the source
    the briefing is expected to trend over later, and the round trip has already
    been paid for here, so storing it now saves a second identical call.

    The write happens after the lines are built and cannot take the section down
    with it. The numbers are already in hand at that point, and a locked database
    is not a reason to drop them from the message.
    """
    try:
        # RuntimeError, which is what fetch() raises for a missing token, is
        # deliberately not in the retry set. An absent whoop_token.json does not
        # resolve itself in ten seconds, and retrying would add half a minute to
        # every briefing for as long as it went unnoticed.
        items = _fetch_with_retry("whoop", whoop.fetch, requests.RequestException)
    except Exception as exc:
        logging.warning("whoop section unavailable: %s", exc)
        return [f"WHOOP unavailable ({exc})"]

    if not items:
        # Expected before the strap syncs after you wake, not a failure. See
        # whoop.fetch() for why the cycle has to close first.
        logging.info("whoop: sleep cycle has not closed yet")
        return ["WHOOP: no completed sleep cycle yet"]

    metrics = items[0]
    lines = whoop.format_lines(metrics)

    try:
        # The dict from whoop.fetch() is already shaped for this, raw payload and
        # all. upsert means re-running the briefing the same morning updates the
        # row rather than failing on the primary key.
        db.upsert_daily_metrics(metrics)
        logging.info("whoop metrics stored for %s", metrics["date"])
    except Exception:
        logging.exception("storing whoop metrics failed, briefing continues")

    return lines


# Cap on coursework lines so the section cannot eat the message. Six covers a
# heavy week; past that the count line says what was cut.
MAX_COURSEWORK_ITEMS = 6


def _coursework_lines() -> list[str]:
    """Coursework section body, or a single unavailable line.

    Called without _fetch_with_retry, alone among the sections: notion._api
    already retries internally, because only it can see the Retry-After header a
    429 carries. Stacking the generic retry on top would make nine attempts and
    add ninety silent seconds to a morning where Notion is actually down.
    """
    try:
        items = notion.fetch()
    except Exception as exc:
        logging.warning("coursework section unavailable: %s", exc)
        return [f"Notion unavailable ({exc})"]

    lines = _format_coursework(items)

    # Same pattern as the WHOOP section: persist after formatting, and never let
    # a database problem cost the message the numbers it already has.
    try:
        for item in items:
            db.upsert_task(item)
        logging.info("stored %d notion task(s)", len(items))
    except Exception:
        logging.exception("storing notion tasks failed, briefing continues")

    return lines


def _format_coursework(items: list[dict]) -> list[str]:
    """Render tasks as lines: overdue flagged first, then dated, then undated.

    fetch() already sorts due date ascending with undated last, which puts
    overdue at the top for free. This function only labels and caps.
    """
    if not items:
        return ["nothing due in the next 14 days"]

    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    lines = []
    for item in items[:MAX_COURSEWORK_ITEMS]:
        due = item["due_date"]
        if due is None:
            label = "no date"
        else:
            due_day = date.fromisoformat(due[:10])
            if due_day < today:
                # The flag word carries the urgency; position alone is not
                # enough on a phone where every line looks the same.
                label = f"OVERDUE {due_day.strftime('%b %d')}"
            elif due_day == today:
                label = "today"
            else:
                label = due_day.strftime("%a %b %d")

        detail = ", ".join(part for part in (item["course"], item["type"]) if part)
        suffix = f" ({detail})" if detail else ""
        lines.append(f"{label}: {item['title'] or '(untitled)'}{suffix}")

    omitted = len(items) - MAX_COURSEWORK_ITEMS
    if omitted > 0:
        lines.append(f"+{omitted} more not shown")
    return lines


def _weather_lines() -> list[str]:
    """Weather section body, or a single unavailable line.

    The broad except is deliberate: this is the degradation boundary. Anything a
    source raises becomes a missing section rather than a lost briefing. Inside
    the source modules, catches stay narrow.
    """
    try:
        items = _fetch_with_retry("weather", weather.fetch, requests.RequestException)
        return [weather.format_line(item) for item in items]
    except Exception as exc:
        logging.warning("weather section unavailable: %s", exc)
        return [f"weather unavailable ({exc})"]


def _news_lines() -> list[str]:
    """News section body, or a single unavailable line."""
    try:
        # RuntimeError is in the retry set on purpose. news.fetch() catches
        # RequestException per feed itself and raises RuntimeError("every news
        # feed failed") when none survive, so a network that is not up yet
        # surfaces here as RuntimeError and never as a requests exception.
        # Retrying only requests exceptions would leave news with no retry at all.
        items = _fetch_with_retry(
            "news", news.fetch, (requests.RequestException, RuntimeError)
        )
        return news.format_lines(items)
    except Exception as exc:
        logging.warning("news section unavailable: %s", exc)
        return [f"news unavailable ({exc})"]


def build_context_block() -> str:
    """Structured facts to hand to the local model in phase 7.

    Keep this as compact labeled plain text, not JSON. Small models follow a simple
    labeled block more reliably than they follow nested structure.
    """
    raise NotImplementedError("phase 7")
