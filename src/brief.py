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
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from src import config
from src.sources import weather, news

# Retry policy for source fetches. The failure being handled is a network adapter
# that has not associated yet after an S3 wake, so backoff is fixed rather than
# exponential: the condition clears on a wall clock timescale of seconds, and
# there is no server being overloaded to back off from.
FETCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 10


def build() -> str:
    """Assemble the full briefing text.

    Phase 2 version: weather and news only, plain template, no LLM.
    Later phases add sections and hand the assembled facts to llm.py for phrasing.
    """
    # Header stamped in local time. Everything stored is UTC per the conventions;
    # this is display, so it converts here.
    today = datetime.now(ZoneInfo(config.TIMEZONE))
    lines = [f"athlete15 briefing, {today.strftime('%a %b %d')}", ""]

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
