"""News headlines via RSS and Atom.

Fetching is done with requests rather than by handing feedparser a URL, because
feedparser.parse() does its own HTTP through urllib and exposes no timeout. This
process is single threaded and the morning job runs unattended, so a feed server
that accepts the connection and then stalls would hang the whole briefing.
Fetching separately also gives us raise_for_status() and the raw response body.

feedparser is still what parses the bytes. It normalises RSS and Atom into one
shape, so nothing below has to care which format a given site publishes.
"""

import calendar
import logging
from datetime import datetime, timezone
from itertools import chain, zip_longest

import feedparser
import requests

from src import config

# Same reasoning as weather.py: an unbounded fetch blocks the whole morning job.
TIMEOUT_SECONDS = 10

# Some publishers reject the default python-requests user agent outright.
USER_AGENT = "athlete15/0.1 (personal briefing bot)"


def fetch() -> list[dict]:
    """Return recent headlines across all configured feeds.

    Feeds are interleaved round robin rather than concatenated, so a prolific
    feed cannot consume all of NEWS_MAX_ITEMS and push the others out entirely.

    Returns:
        A list of dicts with keys: title, link, source, published, raw.

    Raises:
        RuntimeError: if every configured feed failed. Partial failure is not an
            error and returns whatever the surviving feeds gave us.
    """
    per_feed: list[list[dict]] = []

    for url in config.NEWS_FEEDS:
        try:
            response = requests.get(
                url,
                timeout=TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            # Narrow on purpose. RequestException covers timeout, DNS failure,
            # refused connection and non-2xx status, and nothing else. A KeyError
            # from our own parsing below should still crash loudly.
            logging.warning("news feed unreachable: %s (%s)", url, exc)
            continue

        # response_headers lets feedparser use the server's declared charset,
        # which avoids most spurious encoding warnings when parsing raw bytes.
        parsed = feedparser.parse(
            response.content,
            response_headers=dict(response.headers),
        )

        if not parsed.entries:
            logging.warning(
                "news feed returned no entries: %s (%s)",
                url,
                parsed.get("bozo_exception"),
            )
            continue

        if parsed.bozo:
            # Not fatal. feedparser sets bozo for anything from a truncated body
            # to a mismatched charset declaration, then parses what it can. Feeds
            # that are technically malformed but perfectly usable are common, so
            # the real health check is whether entries came back, done above.
            logging.debug(
                "news feed not well formed but usable: %s (%s)",
                url,
                parsed.get("bozo_exception"),
            )

        source = parsed.feed.get("title", url)
        per_feed.append([_to_item(entry, source) for entry in parsed.entries])

    if not per_feed:
        raise RuntimeError("every news feed failed")

    return _interleave(per_feed)[: config.NEWS_MAX_ITEMS]


def format_lines(items: list[dict]) -> list[str]:
    """Return one short string per headline for the briefing."""
    return [f"- {item['title']} ({item['source']})" for item in items]


def _to_item(entry: feedparser.FeedParserDict, source: str) -> dict:
    """Flatten one feedparser entry into the source contract shape."""
    return {
        "title": entry.get("title", "(untitled)"),
        "link": entry.get("link", ""),
        "source": source,
        "published": _published_utc(entry),
        # Raw entry per the CLAUDE.md convention. Note for phase 3: this holds
        # time.struct_time values, which json.dumps cannot serialise. Convert or
        # drop those before writing this to SQLite.
        "raw": dict(entry),
    }


def _published_utc(entry: feedparser.FeedParserDict) -> str | None:
    """Return the entry timestamp as a UTC ISO-8601 string, or None if absent.

    Falls back from published to updated because Atom requires <updated> but
    treats <published> as optional, so Atom feeds frequently have
    updated_parsed and no published_parsed at all. Returns None rather than
    guessing when neither exists, which keeps a dateless entry in the briefing
    instead of silently dropping it.
    """
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct is None:
        return None

    # feedparser normalises these structs to UTC, so timegm is the correct
    # inverse. time.mktime would read the struct as local time and shift every
    # timestamp by the current offset without raising anything.
    epoch = calendar.timegm(struct)
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _interleave(per_feed: list[list[dict]]) -> list[dict]:
    """Merge per-feed lists round robin: first of each, then second of each.

    Each feed already arrives newest first, and this preserves that order within
    a feed. Sorting the merged list by date instead would have to decide where
    the None timestamps go, so this sidesteps the problem.
    """
    # zip_longest pads short feeds with None, which the filter then drops.
    return [item for item in chain.from_iterable(zip_longest(*per_feed)) if item is not None]


if __name__ == "__main__":
    for item in fetch():
        print(item)
