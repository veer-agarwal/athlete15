"""Notion coursework. Phase 6.

Setup:
    1. Create an internal integration at notion.so/my-integrations, copy the secret.
    2. CRITICAL AND EASY TO MISS: open the database in Notion, use the ... menu ->
       Connections, and add your integration. The token by itself grants access to
       nothing. Every database must be shared explicitly.
    3. The database id is the 32 character string in the database URL.

API version note: pinned to 2026-03-11. Version 2025-09-03 split databases into a
database (the container) and one or more data sources (the tables inside it), and
querying moved from /databases/:id/query to /data_sources/:id/query. The id in
.env is the DATABASE id, since that is what the URL shows; the data source id is
resolved from it once and cached for the life of the process.

The Notion API returns deeply nested property objects and every type nests
differently. A title is not a string, it is a list of rich text fragments each
carrying plain_text. The extractors below exist so that nesting is reached
through in exactly one place per type.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

from src import config

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"

# How far ahead fetch() looks for due work. Two weeks covers "psets due next
# week" without dragging in end of semester projects all term.
HORIZON_DAYS = 14

REQUEST_TIMEOUT_SECONDS = 15
RETRY_ATTEMPTS = 3
# Backoff before attempt 2 and attempt 3. Doubling, not fixed like brief.py's
# wake retry: here the failure being handled is Notion telling us to slow down
# or having a bad minute, not a network adapter that is still associating.
BACKOFF_SECONDS = (2, 5)
# Ceiling on how long a Retry-After header is honored. Notion's are single digit
# seconds in practice; a briefing job must not sleep for minutes because a proxy
# somewhere sent a strange header.
MAX_RETRY_AFTER_SECONDS = 60

# The database's data source id, resolved on first use. Keyed by database id so
# a changed .env after a restart cannot serve a stale cache entry.
_data_source_cache: dict[str, str] = {}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _api(method: str, path: str, payload: dict | None = None) -> dict:
    """Call the Notion API with retries. Returns the parsed body.

    Retry policy per the failure type:
        429               wait what Retry-After says, then retry
        5xx               Notion's problem, backoff and retry
        RequestException  timeouts and dead connections, backoff and retry
        other 4xx         our problem (bad token, bad id, bad filter), fail
                          immediately because retrying cannot fix a config error

    Raises:
        RuntimeError: on a 4xx, or when retries are exhausted. The message
            carries Notion's own code and message, because "400" alone tells
            you nothing while "validation_error: body.filter..." names the bug.
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        wait: float = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]
        try:
            response = requests.request(
                method,
                f"{API_BASE}{path}",
                headers=_headers(),
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            if attempt == RETRY_ATTEMPTS:
                raise
            logging.warning("notion attempt %d/%d failed: %s", attempt, RETRY_ATTEMPTS, exc)
            time.sleep(wait)
            continue

        if response.status_code == 429:
            # Retry-After is seconds, and honoring it is not optional courtesy:
            # retrying early just extends the rate limit window.
            try:
                wait = min(float(response.headers.get("Retry-After", wait)),
                           MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                pass
            if attempt == RETRY_ATTEMPTS:
                raise RuntimeError("notion rate limited (429) after retries")
            logging.warning("notion 429, waiting %.1fs as told", wait)
            time.sleep(wait)
            continue

        if response.status_code >= 500:
            if attempt == RETRY_ATTEMPTS:
                raise RuntimeError(f"notion server error {response.status_code} after retries")
            logging.warning("notion %d, attempt %d/%d", response.status_code, attempt, RETRY_ATTEMPTS)
            time.sleep(wait)
            continue

        try:
            body = response.json()
        except ValueError:
            raise RuntimeError(f"notion returned unparseable body: {response.text[:200]}")

        if response.status_code == 404:
            # By design Notion 404s both "wrong id" and "not shared", so it does
            # not leak which pages exist. The second cause is far more common.
            raise RuntimeError(
                f"notion 404 for {path}: almost always means the database is not "
                "shared with the integration (Notion ... menu -> Connections), "
                "not a wrong id"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"notion {response.status_code} {body.get('code')}: {body.get('message')}"
            )
        return body

    raise AssertionError("unreachable")  # every loop path returns, retries, or raises


def _data_source_id() -> str:
    """The data source id behind NOTION_TASKS_DB_ID, cached per database id."""
    database_id = config.NOTION_TASKS_DB_ID
    if database_id not in _data_source_cache:
        body = _api("GET", f"/databases/{database_id}")
        sources = body.get("data_sources") or []
        if not sources:
            raise RuntimeError(f"notion database {database_id} has no data sources")
        if len(sources) > 1:
            # Multi-source databases exist since 2025-09. Taking the first is
            # right for a database this project created, but say something in
            # case that assumption ever breaks.
            logging.warning("notion database has %d data sources, using the first", len(sources))
        _data_source_cache[database_id] = sources[0]["id"]
    return _data_source_cache[database_id]


# ----------------------------------------------------------------------------------
# property extractors
#
# Each takes the property object for one page property, or None when the page
# lacks that property entirely, and returns a plain value. Empty is a value here,
# not an error: a task with no due date is normal and returns None.


def extract_title(prop: dict | None) -> str:
    """Concatenated plain text of a title property.

    Joined across fragments because Notion splits a title on every formatting
    change: a title with one bolded word arrives as three fragments.
    """
    fragments = (prop or {}).get("title") or []
    return "".join(fragment.get("plain_text", "") for fragment in fragments)


def extract_select(prop: dict | None) -> str | None:
    """The selected option's name, or None when nothing is selected."""
    return ((prop or {}).get("select") or {}).get("name")


def extract_status(prop: dict | None) -> str | None:
    """The status option's name, or None when unset."""
    return ((prop or {}).get("status") or {}).get("name")


def extract_date(prop: dict | None) -> str | None:
    """The start of a date property as an ISO string, or None when empty.

    Returned as Notion sent it, which is date only ('2026-07-29') unless a time
    was set in the UI. Callers comparing against local dates slice to [:10].
    """
    return ((prop or {}).get("date") or {}).get("start")


def extract_rich_text(prop: dict | None) -> str | None:
    """Concatenated plain text of a rich_text property, or None when empty.

    No rich_text property exists in the coursework schema today. The extractor
    is here so the set covers every type the schema could plausibly grow, and
    because notes fields are always rich_text when they appear.
    """
    fragments = (prop or {}).get("rich_text") or []
    text = "".join(fragment.get("plain_text", "") for fragment in fragments)
    return text or None


def _parse_page(page: dict) -> dict:
    """Flatten one Notion page into the dict fetch() promises."""
    properties = page.get("properties") or {}
    return {
        "id": page.get("id"),
        "title": extract_title(properties.get("Name")),
        "course": extract_select(properties.get("Course")),
        "due_date": extract_date(properties.get("Due")),
        "status": extract_status(properties.get("Status")),
        "type": extract_select(properties.get("Type")),
        "url": page.get("url"),
        "raw": page,  # full payload, per the storage convention in CLAUDE.md
    }


# ----------------------------------------------------------------------------------
# public source contract


def fetch() -> list[dict]:
    """Return upcoming coursework, due date ascending, undated last.

    Included: anything not Done that is due within HORIZON_DAYS, overdue, or has
    no due date at all. Undated is included on purpose: a task you never dated
    would otherwise be invisible right up until it never got done.

    Returns dicts with keys: id, title, course, due_date, status, type, url, raw.

    Raises:
        RuntimeError: when Notion rejects the request or retries are exhausted.
        requests.RequestException: when the network is down past the retries.
    """
    horizon = (
        datetime.now(ZoneInfo(config.TIMEZONE)).date() + timedelta(days=HORIZON_DAYS)
    ).isoformat()

    payload: dict[str, Any] = {
        "filter": {
            "and": [
                {"property": "Status", "status": {"does_not_equal": "Done"}},
                {"or": [
                    {"property": "Due", "date": {"on_or_before": horizon}},
                    {"property": "Due", "date": {"is_empty": True}},
                ]},
            ]
        },
        "sorts": [{"property": "Due", "direction": "ascending"}],
        # Notion's cap. Fewer means more round trips for nothing.
        "page_size": 100,
    }

    source_id = _data_source_id()
    pages: list[dict] = []
    while True:
        body = _api("POST", f"/data_sources/{source_id}/query", payload)
        pages.extend(body.get("results") or [])
        if not body.get("has_more"):
            break
        # Notion pages with an opaque cursor, not an offset. The same payload
        # goes back with start_cursor set; filter and sorts must be repeated.
        payload["start_cursor"] = body["next_cursor"]

    tasks = [_parse_page(page) for page in pages]
    # Sorted locally rather than trusting the API sort, because the spec for
    # undated items is "last" and Notion's placement of empty dates in a sort is
    # not something worth depending on. False sorts before True, so dated items
    # in date order come first and undated bring up the rear.
    tasks.sort(key=lambda task: (task["due_date"] is None, task["due_date"] or ""))

    logging.info("notion: %d task(s) due by %s or undated", len(tasks), horizon)
    return tasks


if __name__ == "__main__":
    # Run directly to test this module alone:  python -m src.sources.notion
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for task in fetch():
        print(f"{task['due_date'] or 'no date':<12} {task['status'] or '-':<12} "
              f"{task['title']} ({task['course']}, {task['type']})")
