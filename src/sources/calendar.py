"""iCloud calendar, read and single-calendar write, over CalDAV.

Name collision warning: this module shadows the stdlib `calendar` module for
anything that does `import src.sources.calendar`. That is harmless everywhere
else in the project (news.py's own `import calendar` still gets the stdlib
module, because Python 3 imports are absolute by default) but it means this
file itself must never write `import calendar` internally, or it would shadow
itself.

Traps specific to this integration:

- iCloud fronts every account with caldav.icloud.com, then hands real
  requests off to a per-account partition host (something like
  p123-caldav.icloud.com). Never hardcode a calendar or principal URL for
  that reason: discover everything through client.principal().calendars(),
  which is also how the underlying niquests session picks up and follows
  that redirect. A URL that works today can silently 404 after Apple
  reshuffles which partition an account lives on.
- Use an app-specific password from appleid.apple.com, never the Apple ID
  account password itself; CalDAV needs the former and iCloud will still
  accept the latter for a while before rejecting it. Apple also revokes every
  app-specific password automatically the moment the Apple ID password
  changes, so a 401 that shows up weeks after this worked fine is that, not a
  typo in .env.
- caldav 3.x replaced its `requests` HTTP backend with `niquests` (a fork).
  The network-level exceptions this module has to catch therefore live in
  `niquests.exceptions`, not `requests.exceptions`, and code copied from an
  older caldav example that catches `requests.ConnectionError` will silently
  never trigger.
- caldav.lib.error.DAVError subclasses (ReportError, PropfindError, ...) are
  raised for ANY REPORT/PROPFIND/PUT response >= 400 except 401/403 (->
  AuthorizationError), 404 (-> NotFoundError) and 429/503-with-Retry-After
  (-> RateLimitError, see below). That means the identical exception TYPE
  covers both a transient 500 and a malformed 400, with no reliable way to
  tell them apart short of regex-parsing the exception's string form (not a
  documented part of the library's contract). TRANSIENT_ERRORS below
  deliberately excludes those classes for that reason: retrying a real config
  error for the length of brief.py's whole backoff window is worse than
  failing once with a clear message and letting that morning's briefing
  degrade to "calendar unavailable".
- Environment note, not an API trap but it cost real debugging time: the
  installed icalendar_searcher==1.0.6 (a transitive caldav 3.x dependency,
  used internally by Calendar.search()/date_search() for anything beyond a
  bare unfiltered fetch) imports `icalendar.error`, a module that was only
  added in icalendar 6.2.0. requirements.txt had icalendar pinned to 6.1.0
  from before caldav was added to the project, so every call to
  Calendar.search() raised ImportError regardless of what this module did.
  Bumped requirements.txt to icalendar==6.3.2 (the newest 6.x release, to
  avoid an unnecessary jump to the 7.x major) to fix it. Nothing else in the
  project imports icalendar directly, so nothing else was affected.
"""

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import caldav
import niquests
from caldav.lib import error as caldav_error

from src import config

CALDAV_URL = "https://caldav.icloud.com"

# Same reasoning as the timeout in weather.py and whoop.py: a single process
# runs the morning job, so one hung socket must not block it indefinitely.
REQUEST_TIMEOUT_SECONDS = 15

# caldav's own rate-limit handling (see _client() below): the maximum this
# module will let the library sleep, across however many Retry-After-bearing
# 429/503 responses it hits in a row, before giving up and raising
# RateLimitError. Capped so a server asking for an unreasonable delay cannot
# stall the morning job; the resulting RateLimitError is in TRANSIENT_ERRORS,
# so brief.py's own retry loop gets one more attempt from scratch afterward.
RATE_LIMIT_MAX_SLEEP_SECONDS = 30

# Network/server hiccups worth retrying, exported so that brief.py's generic
# retry helper (_fetch_with_retry) never has to import caldav or niquests
# itself. Passed straight to that helper's `retry_on` parameter.
#
#   niquests.exceptions.RequestException
#       Base class covering connection resets, DNS failures, and read/connect
#       timeouts. Nothing about the request was wrong; the network was.
#   caldav.lib.error.RateLimitError
#       A 429, or a 503 carrying Retry-After, that the client's own handling
#       (rate_limit_handle=True, see _client()) could not absorb within
#       RATE_LIMIT_MAX_SLEEP_SECONDS.
#
# caldav.lib.error.AuthorizationError (401/403) is deliberately NOT here: see
# the module docstring and _auth_error() below for why it becomes a
# RuntimeError instead. Every other caldav.lib.error.DAVError subclass is
# also deliberately excluded; see the module docstring for why.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    niquests.exceptions.RequestException,
    caldav_error.RateLimitError,
)


def _client() -> caldav.DAVClient:
    """A CalDAV client authenticated with the configured app-specific password.

    Does no network I/O by itself; the first request happens in whatever
    calls .principal() or .calendars() on the result.
    """
    return caldav.DAVClient(
        url=CALDAV_URL,
        username=config.ICLOUD_USERNAME,
        password=config.ICLOUD_APP_PASSWORD,
        timeout=REQUEST_TIMEOUT_SECONDS,
        rate_limit_handle=True,
        rate_limit_max_sleep=RATE_LIMIT_MAX_SLEEP_SECONDS,
    )


def _auth_error(exc: caldav_error.AuthorizationError) -> RuntimeError:
    """Turn a 401/403 into a RuntimeError naming both possible causes.

    Both are real and indistinguishable from the response alone: iCloud
    returns the same Unauthorized either way, so the message states both
    rather than guessing.
    """
    return RuntimeError(
        "iCloud CalDAV login rejected (401/403). Either (a) a regular Apple "
        "ID password was used where an app-specific password from "
        "appleid.apple.com is required, or (b) the app-specific password was "
        f"automatically revoked when the Apple ID password was later "
        f"changed. Server said: {exc}"
    )


def _discover_calendars(client: caldav.DAVClient) -> list[caldav.Calendar]:
    """All calendars on the account via principal discovery.

    Translates caldav's exceptions at the boundary: auth failures get the
    two-cause message, rate limiting stays raw because it is in
    TRANSIENT_ERRORS and retry loops match on the type, and any other
    DAVError becomes a RuntimeError so no caldav exception type escapes the
    module's documented contract.
    """
    try:
        return client.principal().calendars()
    except caldav_error.AuthorizationError as exc:
        raise _auth_error(exc) from exc
    except caldav_error.RateLimitError:
        raise
    except caldav_error.DAVError as exc:
        raise RuntimeError(f"CalDAV calendar discovery failed: {exc}") from exc


def _display_name(cal: caldav.Calendar) -> str | None:
    """A calendar's display name, or None if the server did not send one.

    Wrapped because get_display_name() does a property fetch that can itself
    fail on a misbehaving calendar; one bad calendar's name should not take
    down the rest of the fetch.
    """
    try:
        return cal.get_display_name()
    except Exception as exc:
        logging.warning("calendar: could not read display name for %s: %s", cal.url, exc)
        return None


# ----------------------------------------------------------------------------------
# reading


def _all_day_bounds(comp: Any) -> tuple[str, str]:
    """(start, end) as plain 'YYYY-MM-DD' strings for an all-day VEVENT.

    An all-day event has no instant, only a date, so there is no UTC instant
    to convert to. DTEND on an all-day event is exclusive per RFC 5545 (the
    day after the last day the event covers); that is preserved as-is rather
    than adjusted, since callers store what iCloud says, not a reinterpretation
    of it.
    """
    start = comp["dtstart"].dt
    dtend = comp.get("dtend")
    end = dtend.dt if dtend is not None else start
    return start.isoformat(), end.isoformat()


def _timed_bounds(comp: Any, tz: ZoneInfo) -> tuple[str, str]:
    """(start, end) as UTC ISO-8601 strings for a timed VEVENT."""
    start = comp["dtstart"].dt
    if start.tzinfo is None:
        # A "floating time" event with no zone of its own. Treating it as
        # config.TIMEZONE rather than UTC is the same call whoop.py makes for
        # a missing offset: local is the far more likely intent for a
        # personal calendar than UTC ever is.
        start = start.replace(tzinfo=tz)

    dtend = comp.get("dtend")
    if dtend is not None:
        end = dtend.dt
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)
    else:
        duration = comp.get("duration")
        end = start + duration.dt if duration is not None else start

    def _to_utc_z(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    return _to_utc_z(start), _to_utc_z(end)


def _row(event: "caldav.CalendarObjectResource", calendar_name: str, tz: ZoneInfo) -> dict:
    """One VEVENT (possibly one recurrence instance) as a plain dict.

    Raises whatever parsing it hits (KeyError on a missing DTSTART, etc.) so
    the caller can log and skip just this one event, per the source contract.
    """
    comp = event.icalendar_component
    all_day = not isinstance(comp["dtstart"].dt, datetime)

    if all_day:
        start_utc, end_utc = _all_day_bounds(comp)
    else:
        start_utc, end_utc = _timed_bounds(comp, tz)

    location = comp.get("location")
    return {
        # str() because caldav hands back icalendar's vText, a str subclass;
        # the source contract says plain types only.
        "uid": str(event.id),
        "summary": str(comp.get("summary", "")),
        "location": str(location) if location else None,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "calendar_name": calendar_name,
        "all_day": all_day,
        # Full iCalendar text, per the store-raw-payload convention. This is
        # what re-parsing later, or debugging a field this function did not
        # extract, falls back on.
        "raw": event.data,
    }


def _sort_key(row: dict) -> tuple[int, str]:
    """All-day events first, then by start time.

    start_utc sorts correctly as a plain string in both cases: all-day rows
    hold 'YYYY-MM-DD' and timed rows hold full ISO-8601 UTC timestamps, both
    lexicographically ordered the same as chronologically.
    """
    return (0 if row["all_day"] else 1, row["start_utc"])


def fetch() -> list[dict]:
    """Today's events across every calendar on the account.

    "Today" is [local midnight, next local midnight) in config.TIMEZONE, and
    the search includes events that only overlap that window (started
    yesterday, ends tomorrow) since CalDAV time-range filtering is an overlap
    test, not a containment test. Recurring events are expanded so today's
    occurrence of a weekly event is returned as its own row.

    A single event that fails to parse is logged and skipped rather than
    failing the whole fetch; a single calendar whose search fails outright
    (and is not a TRANSIENT_ERRORS case, which propagates for brief.py's
    retry loop to see) is likewise logged and skipped.

    Raises:
        RuntimeError: on a 401/403 from iCloud (see _auth_error).
        TRANSIENT_ERRORS: on a network/server hiccup, left unretried here so
            brief.py's retry helper can see the exception type and back off.
    """
    tz = ZoneInfo(config.TIMEZONE)
    start_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)

    client = _client()
    try:
        calendars = _discover_calendars(client)

        rows: list[dict] = []
        for cal in calendars:
            cal_name = _display_name(cal) or "(unnamed calendar)"

            try:
                events = cal.search(
                    event=True, start=start_local, end=end_local, expand=True
                )
            except caldav_error.AuthorizationError as exc:
                raise _auth_error(exc) from exc
            except TRANSIENT_ERRORS:
                raise
            except Exception as exc:
                logging.warning(
                    "calendar %r: search failed, skipping this calendar: %s",
                    cal_name, exc,
                )
                continue

            for event in events:
                try:
                    rows.append(_row(event, cal_name, tz))
                except Exception as exc:
                    logging.warning(
                        "calendar %r: could not parse event uid=%s, skipping: %s",
                        cal_name, getattr(event, "id", "?"), exc,
                    )
    finally:
        client.close()

    rows.sort(key=_sort_key)
    logging.info("calendar: %d event(s) today across %d calendar(s)", len(rows), len(calendars))
    return rows


# ----------------------------------------------------------------------------------
# writing


def create_event(
    title: str,
    start: datetime,
    end: datetime,
    location: str | None = None,
) -> str:
    """Create one event, only in the calendar named config.ICLOUD_CALENDAR_NAME.

    Never falls back to any other calendar: if no calendar has that exact
    display name, raises rather than guessing which one the caller meant.

    There is deliberately no delete or modify function here, even as a
    private helper. Anything that needs to remove or change an event this
    module created belongs to a later phase's write-action code, not this
    read-and-append-only one.

    Args:
        title: becomes the VEVENT SUMMARY.
        start: timezone-aware start time.
        end: timezone-aware end time.
        location: optional VEVENT LOCATION.

    Returns:
        The new event's UID.

    Raises:
        ValueError: if start or end is a naive datetime. A naive datetime
            here would be handed to iCloud with no zone attached, and the
            event would land at whatever hour the server decides to assume
            rather than the one actually intended, with no error to flag it.
        RuntimeError: if no calendar named config.ICLOUD_CALENDAR_NAME exists
            (message lists the calendars that do), if more than one calendar
            has that name, on a 401/403 from iCloud (see _auth_error), or on
            any other CalDAV protocol failure.
        TRANSIENT_ERRORS: network hiccups and rate limiting propagate raw so
            a caller with a retry loop can match on the type.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError(
            "create_event requires timezone-aware start/end datetimes, got "
            f"start.tzinfo={start.tzinfo!r} end.tzinfo={end.tzinfo!r}"
        )

    client = _client()
    try:
        calendars = _discover_calendars(client)

        names = [_display_name(cal) for cal in calendars]
        matches = [
            cal for cal, name in zip(calendars, names)
            if name == config.ICLOUD_CALENDAR_NAME
        ]
        if not matches:
            existing = sorted(name for name in names if name)
            raise RuntimeError(
                f"no calendar named {config.ICLOUD_CALENDAR_NAME!r} found. "
                f"Calendars on this account: {existing}"
            )
        if len(matches) > 1:
            # iCloud allows two calendars with the same display name, and the
            # server's ordering of them is not stable. Writing to whichever
            # happened to come back last would be a silent guess.
            raise RuntimeError(
                f"{len(matches)} calendars are named "
                f"{config.ICLOUD_CALENDAR_NAME!r}; refusing to guess between "
                "them. Rename one in the Calendar app."
            )
        target = matches[0]

        event_kwargs: dict[str, Any] = {"dtstart": start, "dtend": end, "summary": title}
        if location:
            event_kwargs["location"] = location

        try:
            event = target.add_event(**event_kwargs)
        except caldav_error.AuthorizationError as exc:
            raise _auth_error(exc) from exc
        except caldav_error.RateLimitError:
            raise
        except caldav_error.DAVError as exc:
            raise RuntimeError(f"CalDAV event creation failed: {exc}") from exc

        logging.info("calendar: created event uid=%s in %r", event.id, config.ICLOUD_CALENDAR_NAME)
        return str(event.id)
    finally:
        client.close()


# ----------------------------------------------------------------------------------
# time resolution
#
# This section is a deliberate exception to the fetch-shaped source contract
# above: it does no network I/O and returns no dicts, and it lives here
# anyway because the router needs exactly one place that owns "what does the
# user's partial date/time text mean as an aware datetime", and that meaning
# (the local timezone, what a missing end time defaults to) is calendar
# semantics, not router logic.


def resolve_time(
    date_str: str | None,
    start_str: str | None,
    end_str: str | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Turn router-extracted date/time strings into aware (start, end) datetimes.

    Args:
        date_str: "YYYY-MM-DD", or None to mean the date of `now`.
        start_str: "HH:MM" or "H:MM" 24h, required (None raises).
        end_str: "HH:MM" or "H:MM" 24h, or None to mean start + 60 minutes.
        now: the clock to use when date_str is None. Defaults to the current
            local time; the parameter exists so tests can pin it instead of
            depending on wall clock time.

    Returns:
        (start, end) as timezone-aware datetimes in ZoneInfo(config.TIMEZONE).

    Raises:
        ValueError: date_str/start_str/end_str is present but unparseable
            (message names which field), start_str is None, or the resolved
            end is not after the resolved start.
    """
    tz = ZoneInfo(config.TIMEZONE)

    if date_str is None:
        the_date = (now or datetime.now(tz)).date()
    else:
        try:
            the_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"unparseable date: {date_str!r}") from exc

    if start_str is None:
        raise ValueError("start_str is required (got None)")
    start_time = _parse_hhmm("start_str", start_str)

    start = datetime.combine(the_date, start_time, tzinfo=tz)

    if end_str is None:
        end = start + timedelta(minutes=60)
    else:
        end_time = _parse_hhmm("end_str", end_str)
        end = datetime.combine(the_date, end_time, tzinfo=tz)

    if end <= start:
        raise ValueError(
            f"end ({end.isoformat()}) is not after start ({start.isoformat()})"
        )

    return start, end


def _parse_hhmm(field_name: str, value: str) -> time:
    """Parse "HH:MM" or "H:MM" 24h into a time, naming the bad field on failure.

    %-H (no leading zero) is not portable to Windows' strptime, so a single
    digit hour like "9:05" is zero-padded and retried rather than handled via
    format string alone.
    """
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        pass
    # Accept "9:05" style single-digit hours by zero-padding before retrying,
    # since Windows' strptime (unlike glibc) does not support %-H.
    parts = value.split(":")
    if len(parts) == 2 and len(parts[0]) == 1 and parts[0].isdigit():
        try:
            return datetime.strptime(f"0{value}", "%H:%M").time()
        except ValueError:
            pass
    raise ValueError(f"unparseable {field_name}: {value!r}")


if __name__ == "__main__":
    # Run directly to test this module alone:  python -m src.sources.calendar
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    for row in fetch():
        shown = {k: v for k, v in row.items() if k != "raw"}
        print(shown)
