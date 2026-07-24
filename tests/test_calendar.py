"""Unit tests for src/sources/calendar.py.

No network. Real icalendar.Event components are built in-process to exercise the
parsing helpers, and caldav's client/principal/calendar/event objects are stood
in for with minimal fakes exposing only the attributes/methods calendar.py
actually touches (.principal(), .calendars(), .search(), .get_display_name(),
.id, .data, .icalendar_component, .close()).

Run with:  pytest tests/test_calendar.py
"""

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import icalendar
import niquests
import pytest
from caldav.lib import error as caldav_error

from src.sources import calendar

NY = ZoneInfo("America/New_York")


# --- fakes -------------------------------------------------------------------------


class FakeCaldavEvent:
    """Stand-in for caldav.CalendarObjectResource: only what _row() touches."""

    def __init__(self, id: str, comp: icalendar.Event, data: str = "RAW-ICS-DATA"):
        self.id = id
        self.icalendar_component = comp
        self.data = data


class FakeCalendar:
    """Stand-in for caldav.Calendar: only what fetch() touches."""

    def __init__(self, name: str, events=None, search_error: Exception | None = None):
        self.name = name
        self.url = f"https://example.invalid/{name}"
        self._events = events if events is not None else []
        self._search_error = search_error

    def get_display_name(self) -> str:
        return self.name

    def search(self, **kwargs):
        if self._search_error is not None:
            raise self._search_error
        return self._events


class FakePrincipal:
    def __init__(self, calendars):
        self._calendars = calendars

    def calendars(self):
        return self._calendars


class FakeClient:
    """Stand-in for caldav.DAVClient: only what fetch()/create_event() touch."""

    def __init__(self, principal=None, principal_error: Exception | None = None):
        self._principal = principal
        self._principal_error = principal_error
        self.closed = False

    def principal(self):
        if self._principal_error is not None:
            raise self._principal_error
        return self._principal

    def close(self):
        self.closed = True


def _timed_comp(
    summary: str | None = "Test",
    location: str | None = "Loc",
    start: datetime | None = None,
    end: datetime | None = None,
    duration: timedelta | None = None,
) -> icalendar.Event:
    comp = icalendar.Event()
    if summary is not None:
        comp.add("summary", summary)
    if location is not None:
        comp.add("location", location)
    comp.add("dtstart", start)
    if end is not None:
        comp.add("dtend", end)
    if duration is not None:
        comp.add("duration", duration)
    return comp


def _all_day_comp(start: date, end: date | None = None) -> icalendar.Event:
    comp = icalendar.Event()
    comp.add("summary", "All day event")
    comp.add("dtstart", start)
    if end is not None:
        comp.add("dtend", end)
    return comp


# --- _row: timed events --------------------------------------------------------


def test_row_timed_event_full_shape():
    start = datetime(2026, 7, 24, 15, 0, tzinfo=NY)
    end = datetime(2026, 7, 24, 16, 0, tzinfo=NY)
    comp = _timed_comp(summary="Lift", location="facility", start=start, end=end)
    event = FakeCaldavEvent("uid-1", comp, data="BEGIN:VCALENDAR...raw...END:VCALENDAR")

    row = calendar._row(event, "Home", NY)

    assert row["uid"] == "uid-1"
    assert row["summary"] == "Lift"
    assert row["location"] == "facility"
    assert row["start_utc"] == "2026-07-24T19:00:00Z"
    assert row["end_utc"] == "2026-07-24T20:00:00Z"
    assert row["calendar_name"] == "Home"
    assert row["all_day"] is False
    assert row["raw"] == event.data


def test_row_missing_location_is_none():
    start = datetime(2026, 7, 24, 15, 0, tzinfo=NY)
    comp = _timed_comp(summary="Lift", location=None, start=start, end=start)
    event = FakeCaldavEvent("uid-2", comp)

    row = calendar._row(event, "Home", NY)

    assert row["location"] is None


def test_row_missing_summary_is_empty_string():
    start = datetime(2026, 7, 24, 15, 0, tzinfo=NY)
    comp = _timed_comp(summary=None, location=None, start=start, end=start)
    event = FakeCaldavEvent("uid-3", comp)

    row = calendar._row(event, "Home", NY)

    assert row["summary"] == ""


# --- _row: all-day events -------------------------------------------------------


def test_row_all_day_event():
    comp = _all_day_comp(date(2026, 7, 24), date(2026, 7, 25))
    event = FakeCaldavEvent("uid-4", comp)

    row = calendar._row(event, "Home", NY)

    assert row["all_day"] is True
    assert row["start_utc"] == "2026-07-24"
    assert row["end_utc"] == "2026-07-25"


# --- _timed_bounds ---------------------------------------------------------------


def test_timed_bounds_aware_datetime_converted_to_utc():
    start = datetime(2026, 7, 24, 15, 0, tzinfo=NY)
    end = datetime(2026, 7, 24, 16, 0, tzinfo=NY)
    comp = _timed_comp(start=start, end=end)

    start_utc, end_utc = calendar._timed_bounds(comp, NY)

    assert start_utc == "2026-07-24T19:00:00Z"
    assert end_utc == "2026-07-24T20:00:00Z"


def test_timed_bounds_naive_datetime_treated_as_given_tz():
    """A floating-time DTSTART/DTEND has no tzinfo; it must be localized to the
    tz passed in (what callers pass as config.TIMEZONE), not treated as UTC."""
    start = datetime(2026, 7, 24, 15, 0)
    end = datetime(2026, 7, 24, 16, 0)
    comp = _timed_comp(start=start, end=end)

    start_utc, end_utc = calendar._timed_bounds(comp, NY)

    assert start_utc == "2026-07-24T19:00:00Z"
    assert end_utc == "2026-07-24T20:00:00Z"


def test_timed_bounds_missing_dtend_uses_duration():
    start = datetime(2026, 7, 24, 15, 0, tzinfo=NY)
    comp = _timed_comp(start=start, end=None, duration=timedelta(hours=1, minutes=30))

    start_utc, end_utc = calendar._timed_bounds(comp, NY)

    assert start_utc == "2026-07-24T19:00:00Z"
    assert end_utc == "2026-07-24T20:30:00Z"


def test_timed_bounds_missing_dtend_and_duration_end_equals_start():
    start = datetime(2026, 7, 24, 15, 0, tzinfo=NY)
    comp = _timed_comp(start=start, end=None, duration=None)

    start_utc, end_utc = calendar._timed_bounds(comp, NY)

    assert start_utc == end_utc == "2026-07-24T19:00:00Z"


# --- _all_day_bounds ---------------------------------------------------------------


def test_all_day_bounds_dtend_exclusive_date_preserved_as_is():
    comp = _all_day_comp(date(2026, 7, 24), date(2026, 7, 25))
    start, end = calendar._all_day_bounds(comp)
    assert start == "2026-07-24"
    assert end == "2026-07-25"


def test_all_day_bounds_missing_dtend_end_equals_start():
    comp = _all_day_comp(date(2026, 7, 24))
    start, end = calendar._all_day_bounds(comp)
    assert start == end == "2026-07-24"


# --- _sort_key ---------------------------------------------------------------------


def test_sort_key_all_day_before_timed_then_by_start():
    row_timed_late = {"all_day": False, "start_utc": "2026-07-24T19:00:00Z"}
    row_all_day = {"all_day": True, "start_utc": "2026-07-24"}
    row_timed_early = {"all_day": False, "start_utc": "2026-07-24T13:00:00Z"}

    rows = [row_timed_late, row_all_day, row_timed_early]
    rows.sort(key=calendar._sort_key)

    assert rows == [row_all_day, row_timed_early, row_timed_late]


# --- _auth_error ---------------------------------------------------------------------


def test_auth_error_message_covers_both_causes():
    exc = caldav_error.AuthorizationError(reason="401 Unauthorized")
    err = calendar._auth_error(exc)

    assert isinstance(err, RuntimeError)
    message = str(err)
    assert "app-specific password" in message
    assert "revoked" in message


# --- create_event validation -------------------------------------------------------


def test_create_event_raises_on_naive_start_without_touching_client():
    naive_start = datetime(2026, 7, 24, 15, 0)
    aware_end = datetime(2026, 7, 24, 16, 0, tzinfo=NY)

    with patch(
        "src.sources.calendar._client",
        side_effect=AssertionError("_client must not be called before validation"),
    ):
        with pytest.raises(ValueError):
            calendar.create_event("Title", naive_start, aware_end)


def test_create_event_raises_on_naive_end_without_touching_client():
    aware_start = datetime(2026, 7, 24, 15, 0, tzinfo=NY)
    naive_end = datetime(2026, 7, 24, 16, 0)

    with patch(
        "src.sources.calendar._client",
        side_effect=AssertionError("_client must not be called before validation"),
    ):
        with pytest.raises(ValueError):
            calendar.create_event("Title", aware_start, naive_end)


# --- TRANSIENT_ERRORS ---------------------------------------------------------------


def test_transient_errors_contains_niquests_request_exception():
    assert niquests.exceptions.RequestException in calendar.TRANSIENT_ERRORS


def test_transient_errors_contains_rate_limit_error():
    assert caldav_error.RateLimitError in calendar.TRANSIENT_ERRORS


def test_transient_errors_excludes_authorization_error():
    assert caldav_error.AuthorizationError not in calendar.TRANSIENT_ERRORS


# --- fetch() ---------------------------------------------------------------------


def test_fetch_skips_calendar_whose_search_raises_generic_exception():
    good_comp = _timed_comp(
        summary="Good",
        start=datetime(2026, 7, 24, 15, 0, tzinfo=NY),
        end=datetime(2026, 7, 24, 16, 0, tzinfo=NY),
    )
    bad_cal = FakeCalendar("Bad", search_error=Exception("500 whatever"))
    good_cal = FakeCalendar("Good", events=[FakeCaldavEvent("good-uid", good_comp)])
    fake_client = FakeClient(principal=FakePrincipal([bad_cal, good_cal]))

    with patch("src.sources.calendar._client", return_value=fake_client):
        rows = calendar.fetch()

    assert len(rows) == 1
    assert rows[0]["uid"] == "good-uid"


def test_fetch_skips_event_whose_parse_raises():
    bad_comp = icalendar.Event()  # no DTSTART -> KeyError inside _row
    good_comp = _timed_comp(
        summary="Good",
        start=datetime(2026, 7, 24, 15, 0, tzinfo=NY),
        end=datetime(2026, 7, 24, 16, 0, tzinfo=NY),
    )
    cal = FakeCalendar(
        "Cal",
        events=[FakeCaldavEvent("bad-uid", bad_comp), FakeCaldavEvent("good-uid", good_comp)],
    )
    fake_client = FakeClient(principal=FakePrincipal([cal]))

    with patch("src.sources.calendar._client", return_value=fake_client):
        rows = calendar.fetch()

    assert len(rows) == 1
    assert rows[0]["uid"] == "good-uid"


def test_fetch_authorization_error_from_principal_becomes_runtime_error():
    fake_client = FakeClient(principal_error=caldav_error.AuthorizationError(reason="401"))

    with patch("src.sources.calendar._client", return_value=fake_client):
        with pytest.raises(RuntimeError):
            calendar.fetch()
