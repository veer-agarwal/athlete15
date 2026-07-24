"""Unit tests for src/sources/whoop.py: token persistence and auth-error routing.

No network: every test either only touches the filesystem via a tmp_path token
file, or replaces _authorized_client with a fake object so the real WhoopClient
and authlib never make a request.

Run with:  pytest tests/test_whoop.py
"""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
import requests
from authlib.integrations.base_client.errors import OAuthError

from src import config, db
from src.sources import whoop


# --- _save_token ---------------------------------------------------------------


def test_save_token_writes_new_and_backs_up_old(tmp_path, monkeypatch):
    token_path = tmp_path / "whoop_token.json"
    token_path.write_text(json.dumps({"access_token": "OLD"}), encoding="utf-8")
    monkeypatch.setattr(config, "WHOOP_TOKEN_PATH", token_path)

    whoop._save_token({"access_token": "NEW"})

    assert json.loads(token_path.read_text(encoding="utf-8")) == {"access_token": "NEW"}
    bak_path = token_path.with_name(token_path.name + ".bak")
    assert json.loads(bak_path.read_text(encoding="utf-8")) == {"access_token": "OLD"}


def test_save_token_first_write_creates_no_backup_and_does_not_raise(tmp_path, monkeypatch):
    token_path = tmp_path / "whoop_token.json"
    monkeypatch.setattr(config, "WHOOP_TOKEN_PATH", token_path)
    assert not token_path.exists()

    whoop._save_token({"access_token": "NEW"})  # must not raise

    assert json.loads(token_path.read_text(encoding="utf-8")) == {"access_token": "NEW"}
    bak_path = token_path.with_name(token_path.name + ".bak")
    assert not bak_path.exists()


def test_save_token_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    token_path = tmp_path / "whoop_token.json"
    monkeypatch.setattr(config, "WHOOP_TOKEN_PATH", token_path)

    whoop._save_token({"access_token": "NEW"})

    temp_path = token_path.with_name(token_path.name + ".tmp")
    assert not temp_path.exists()


# --- auth error separation: fetch() and fetch_workouts() ------------------------
#
# WHOOP rejecting the stored token (OAuthError from an auto-refresh mid-request)
# must surface as whoop.WhoopAuthError so callers know not to retry. A plain
# network fault (requests.RequestException) must propagate unchanged so the
# generic retry-on-network-failure path in brief.py still applies to it. Mixing
# the two up is exactly the bug CLAUDE.md calls out: a misdiagnosed timing
# problem where a real auth outage looked like "still waiting on the network".


class _FakeClient:
    """Stands in for WhoopClient. Only the methods fetch()/fetch_workouts() call
    are implemented; each raises on demand so the exception path is deterministic."""

    def __init__(self, raise_exc: Exception | None = None) -> None:
        self._raise_exc = raise_exc
        self.closed = False

    def get_cycle_collection(self, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        return []

    def get_workout_collection(self, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        return []

    def close(self) -> None:
        self.closed = True


def test_fetch_wraps_oauth_error_as_whoop_auth_error(monkeypatch):
    fake = _FakeClient(raise_exc=OAuthError("invalid_grant", "token revoked"))
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    with pytest.raises(whoop.WhoopAuthError):
        whoop.fetch()

    assert fake.closed is True


def test_fetch_does_not_leak_raw_oauth_error(monkeypatch):
    """The caller must see WhoopAuthError, never the underlying authlib type."""
    fake = _FakeClient(raise_exc=OAuthError("invalid_grant", "token revoked"))
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    try:
        whoop.fetch()
        assert False, "expected an exception"
    except OAuthError:
        pytest.fail("OAuthError leaked out of fetch() instead of being wrapped")
    except whoop.WhoopAuthError:
        pass


def test_fetch_leaves_request_exception_unchanged(monkeypatch):
    fake = _FakeClient(raise_exc=requests.ConnectionError("adapter not up yet"))
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    with pytest.raises(requests.RequestException):
        whoop.fetch()

    assert fake.closed is True


def test_fetch_request_exception_is_not_whoop_auth_error(monkeypatch):
    fake = _FakeClient(raise_exc=requests.ConnectionError("adapter not up yet"))
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    try:
        whoop.fetch()
        assert False, "expected an exception"
    except whoop.WhoopAuthError:
        pytest.fail("a network fault must not be misreported as an auth failure")
    except requests.RequestException:
        pass


def test_fetch_workouts_wraps_oauth_error_as_whoop_auth_error(monkeypatch):
    fake = _FakeClient(raise_exc=OAuthError("invalid_grant", "token revoked"))
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    with pytest.raises(whoop.WhoopAuthError):
        whoop.fetch_workouts()

    assert fake.closed is True


def test_fetch_workouts_leaves_request_exception_unchanged(monkeypatch):
    fake = _FakeClient(raise_exc=requests.ConnectionError("adapter not up yet"))
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    with pytest.raises(requests.RequestException):
        whoop.fetch_workouts()

    assert fake.closed is True


def test_fetch_workouts_request_exception_is_not_whoop_auth_error(monkeypatch):
    fake = _FakeClient(raise_exc=requests.ConnectionError("adapter not up yet"))
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    try:
        whoop.fetch_workouts()
        assert False, "expected an exception"
    except whoop.WhoopAuthError:
        pytest.fail("a network fault must not be misreported as an auth failure")
    except requests.RequestException:
        pass


# --- _local_dt_str ---------------------------------------------------------------


def test_local_dt_str_formats_in_the_given_offset():
    # 23:30 UTC with a -04:00 offset is 19:30 local, same calendar day.
    assert whoop._local_dt_str("2026-07-23T23:30:00.000Z", "-04:00") == "2026-07-23 19:30"


def test_local_dt_str_none_timestamp_is_a_dash():
    assert whoop._local_dt_str(None, "-04:00") == "-"


# --- fetch_workouts: sport_name --------------------------------------------------


class _FakeWorkoutClient:
    """Stands in for WhoopClient.get_workout_collection with one fixed payload."""

    def __init__(self, workouts: list[dict]) -> None:
        self._workouts = workouts
        self.closed = False

    def get_workout_collection(self, **kwargs):
        return self._workouts

    def close(self) -> None:
        self.closed = True


def test_fetch_workouts_includes_sport_name_from_payload(monkeypatch):
    fake = _FakeWorkoutClient([{
        "id": "w1",
        "start": "2026-07-23T20:00:00.000Z",
        "end": "2026-07-23T21:30:00.000Z",
        "timezone_offset": "-04:00",
        "sport_id": 34,
        "sport_name": "volleyball",
        "score_state": "SCORED",
        "score": {
            "strain": 12.0, "average_heart_rate": 140, "max_heart_rate": 170,
            "kilojoule": 900.0,
        },
    }])
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    rows = whoop.fetch_workouts()

    assert rows[0]["sport_name"] == "volleyball"
    assert rows[0]["sport_id"] == 34


# --- audit() -----------------------------------------------------------------------


class _FakeAuditClient:
    """Stands in for WhoopClient across audit()'s three collection calls."""

    def __init__(self, cycles=None, recoveries=None, sleeps=None, raise_exc=None) -> None:
        self._cycles = cycles or []
        self._recoveries = recoveries or []
        self._sleeps = sleeps or []
        self._raise_exc = raise_exc
        self.closed = False

    def get_cycle_collection(self, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._cycles

    def get_recovery_collection(self, **kwargs):
        return self._recoveries

    def get_sleep_collection(self, **kwargs):
        return self._sleeps

    def close(self) -> None:
        self.closed = True


def _audit_sleep(sleep_id: str, end_utc: str, start_utc: str | None = None) -> dict:
    return {
        "id": sleep_id,
        "score_state": "SCORED",
        "start": start_utc,
        "end": end_utc,
        "timezone_offset": "-04:00",
        "score": {
            "stage_summary": {
                "total_in_bed_time_milli": 28_800_000,
                "total_awake_time_milli": 0,
            },
            "sleep_performance_percentage": 90,
        },
    }


def test_metric_date_without_sleep_labels_by_cycle_start_not_end():
    """A cycle with no scored sleep is labeled by its START.

    Regression for real cycle 1644738825, which ran 2026-07-16 01:09 to
    2026-07-17 02:35 local and was labeled 07-17 by its end. That is the day the
    NEXT cycle starts, so the label collided with its successor and shifted the
    whole week forward.
    """
    cycle = {
        "id": 1644738825,
        "start": "2026-07-16T05:09:00.000Z",  # local 2026-07-16 01:09
        "end": "2026-07-17T06:35:00.000Z",    # local 2026-07-17 02:35
        "timezone_offset": "-04:00",
    }
    assert whoop._metric_date(cycle, None) == "2026-07-16"


def test_metric_date_prefers_sleep_end_over_cycle_start():
    """With a scored sleep, the wake instant still wins.

    Cycle start is a bedtime; on a pre-midnight bedtime it lands on the previous
    calendar day, while the sleep end is the morning you actually woke up.
    """
    cycle = {
        "start": "2026-07-23T03:00:00.000Z",  # local 2026-07-22 23:00
        "end": "2026-07-24T03:00:00.000Z",
        "timezone_offset": "-04:00",
    }
    sleep = {
        "start": "2026-07-23T03:00:00.000Z",  # opens the cycle
        "end": "2026-07-23T11:00:00.000Z",    # local 07:00
        "timezone_offset": "-04:00",
    }
    assert whoop._metric_date(cycle, sleep) == "2026-07-23"


def test_audit_buckets_workout_by_cycle_containment_not_date(monkeypatch):
    cycles = [
        {
            "id": "cycle1",
            "start": "2026-07-22T23:00:00.000Z",
            "end": "2026-07-23T13:00:00.000Z",
            "timezone_offset": "-04:00",
            "score_state": "SCORED",
            "score": {"strain": 10.0},
        },
        {
            "id": "cycle2",
            "start": "2026-07-23T23:00:00.000Z",
            "end": "2026-07-24T13:00:00.000Z",
            "timezone_offset": "-04:00",
            "score_state": "SCORED",
            "score": {"strain": 5.0},
        },
    ]
    recoveries = [
        {"cycle_id": "cycle1", "sleep_id": "sleep1", "score_state": "SCORED",
         "score": {"recovery_score": 60}},
        {"cycle_id": "cycle2", "sleep_id": "sleep2", "score_state": "SCORED",
         "score": {"recovery_score": 70}},
    ]
    sleeps = [
        _audit_sleep("sleep1", "2026-07-23T10:00:00.000Z"),  # local 06:00 -> 07-23
        _audit_sleep("sleep2", "2026-07-24T10:00:00.000Z"),  # local 06:00 -> 07-24
    ]
    fake = _FakeAuditClient(cycles=cycles, recoveries=recoveries, sleeps=sleeps)
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)
    monkeypatch.setattr(
        whoop, "fetch_workouts",
        lambda start_date=None: [
            {
                # Local date 07-22, which does NOT match cycle1's assigned label
                # of 07-23, but the start instant falls inside cycle1's window.
                # Containment must place it here; date equality would not.
                "id": "w-inside",
                "date": "2026-07-22",
                "sport_name": "volleyball",
                "sport_id": 34,
                "duration_min": 90,
                "strain": 12.0,
                "start_utc": "2026-07-23T02:00:00.000Z",
            },
            {
                # Sits in the gap between cycle1's end and cycle2's start, and
                # its local date matches cycle1's label. It belongs to neither
                # cycle and must not be filed onto one on the strength of the
                # date alone.
                "id": "w-gap",
                "date": "2026-07-23",
                "sport_name": "golf",
                "sport_id": 22,
                "duration_min": 60,
                "strain": 4.0,
                "start_utc": "2026-07-23T20:00:00.000Z",
            },
        ],
    )

    rows = whoop.audit(days=7)
    by_date = {row["date"]: row for row in rows}

    assert set(by_date) == {"2026-07-23", "2026-07-24"}

    day1 = by_date["2026-07-23"]
    assert day1["cycle_id"] == "cycle1"
    assert day1["strain"] == 10.0
    assert day1["recovery_score"] == 60
    assert day1["sleep_hours"] == 8.0
    assert day1["sleep_performance"] == 90
    assert day1["start_local"] == "2026-07-22 19:00"
    assert day1["end_local"] == "2026-07-23 09:00"
    assert day1["workouts"] == [
        {"sport": "volleyball", "duration_min": 90, "strain": 12.0}
    ]

    # The cycle with no contained workout gets an empty list, not an omitted key
    # or None. The gap workout is filed nowhere rather than onto both cycles.
    day2 = by_date["2026-07-24"]
    assert day2["workouts"] == []


def test_audit_oauth_error_raises_whoop_auth_error(monkeypatch):
    fake = _FakeAuditClient(raise_exc=OAuthError("invalid_grant", "token revoked"))
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)
    monkeypatch.setattr(whoop, "fetch_workouts", lambda start_date=None: [])

    with pytest.raises(whoop.WhoopAuthError):
        whoop.audit(days=7)

    assert fake.closed is True


def test_audit_tags_which_cycle_the_briefing_reads_each_field_from(monkeypatch):
    """The open cycle is 'current', the newest closed-and-scored one 'completed'.

    Mirrors the real morning: cycle 1664667247 opened at 01:24 today and carries
    last night's sleep, while the cycle that closed at 01:24 carries yesterday's
    strain. Anything older is not read by the briefing at all.
    """
    cycles = [
        {
            "id": "open", "start": "2026-07-24T05:24:00.000Z", "end": None,
            "timezone_offset": "-04:00", "score_state": "PENDING_SCORE",
            "score": {"strain": 3.2},
        },
        {
            "id": "done", "start": "2026-07-23T05:09:00.000Z",
            "end": "2026-07-24T05:24:00.000Z", "timezone_offset": "-04:00",
            "score_state": "SCORED", "score": {"strain": 14.2},
        },
        {
            "id": "older", "start": "2026-07-22T04:30:00.000Z",
            "end": "2026-07-23T05:09:00.000Z", "timezone_offset": "-04:00",
            "score_state": "SCORED", "score": {"strain": 9.0},
        },
    ]
    recoveries = [
        {"cycle_id": "open", "sleep_id": "s-open", "score_state": "SCORED",
         "score": {"recovery_score": 79}},
        {"cycle_id": "done", "sleep_id": "s-done", "score_state": "SCORED",
         "score": {"recovery_score": 41}},
        {"cycle_id": "older", "sleep_id": "s-older", "score_state": "SCORED",
         "score": {"recovery_score": 55}},
    ]
    sleeps = [
        _audit_sleep("s-open", "2026-07-24T12:54:00.000Z", "2026-07-24T05:24:00.000Z"),
        _audit_sleep("s-done", "2026-07-23T10:09:00.000Z", "2026-07-23T05:09:00.000Z"),
        _audit_sleep("s-older", "2026-07-22T09:30:00.000Z", "2026-07-22T04:30:00.000Z"),
    ]
    fake = _FakeAuditClient(cycles=cycles, recoveries=recoveries, sleeps=sleeps)
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)
    monkeypatch.setattr(whoop, "fetch_workouts", lambda start_date=None: [])

    roles = {row["cycle_id"]: row["cycle_kind"] for row in whoop.audit(days=7)}

    assert roles == {"open": "current", "done": "completed", "older": None}


def test_audit_completed_role_skips_a_cycle_with_no_scored_recovery(monkeypatch):
    """fetch() walks past a closed cycle WHOOP never scored, so the audit must too.

    Otherwise the table would name a cycle as the strain source that the briefing
    silently skipped, which is the opposite of what the mapping line is for.
    """
    cycles = [
        {
            "id": "unscored", "start": "2026-07-23T05:09:00.000Z",
            "end": "2026-07-24T05:24:00.000Z", "timezone_offset": "-04:00",
            "score_state": "SCORED", "score": {"strain": 14.2},
        },
        {
            "id": "usable", "start": "2026-07-22T04:30:00.000Z",
            "end": "2026-07-23T05:09:00.000Z", "timezone_offset": "-04:00",
            "score_state": "SCORED", "score": {"strain": 9.0},
        },
    ]
    recoveries = [
        {"cycle_id": "usable", "sleep_id": "s-usable", "score_state": "SCORED",
         "score": {"recovery_score": 55}},
    ]
    sleeps = [
        _audit_sleep("s-usable", "2026-07-22T09:30:00.000Z", "2026-07-22T04:30:00.000Z"),
    ]
    fake = _FakeAuditClient(cycles=cycles, recoveries=recoveries, sleeps=sleeps)
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)
    monkeypatch.setattr(whoop, "fetch_workouts", lambda start_date=None: [])

    roles = {row["cycle_id"]: row["cycle_kind"] for row in whoop.audit(days=7)}

    assert roles == {"unscored": None, "usable": "completed"}


def test_audit_current_role_withheld_until_the_open_cycle_is_scored(monkeypatch):
    """Before the strap syncs, fetch() returns no current row at all, so no cycle
    feeds the header and the table must not claim one does."""
    cycles = [
        {
            "id": "open", "start": "2026-07-24T05:24:00.000Z", "end": None,
            "timezone_offset": "-04:00", "score_state": "PENDING_SCORE", "score": {},
        },
        {
            "id": "done", "start": "2026-07-23T05:09:00.000Z",
            "end": "2026-07-24T05:24:00.000Z", "timezone_offset": "-04:00",
            "score_state": "SCORED", "score": {"strain": 14.2},
        },
    ]
    recoveries = [
        {"cycle_id": "done", "sleep_id": "s-done", "score_state": "SCORED",
         "score": {"recovery_score": 41}},
    ]
    sleeps = [
        _audit_sleep("s-done", "2026-07-23T10:09:00.000Z", "2026-07-23T05:09:00.000Z"),
    ]
    fake = _FakeAuditClient(cycles=cycles, recoveries=recoveries, sleeps=sleeps)
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)
    monkeypatch.setattr(whoop, "fetch_workouts", lambda start_date=None: [])

    roles = {row["cycle_id"]: row["cycle_kind"] for row in whoop.audit(days=7)}

    assert roles == {"open": None, "done": "completed"}


def test_audit_resolves_the_opening_sleep_when_recovery_has_no_sleep_id(monkeypatch):
    """fetch() falls back to the cycle's own sleep endpoint, so audit must too.

    Otherwise audit skips a cycle fetch() accepts and names an older one as the
    strain source, which is the exact drift the cycle_kind column rules out.
    """
    cycles = [
        {
            "id": "done", "start": "2026-07-23T05:09:00.000Z",
            "end": "2026-07-24T05:24:00.000Z", "timezone_offset": "-04:00",
            "score_state": "SCORED", "score": {"strain": 14.2},
        },
    ]
    recoveries = [
        # No sleep_id, which older WHOOP records can lack.
        {"cycle_id": "done", "score_state": "SCORED", "score": {"recovery_score": 41}},
    ]
    sleeps = [
        _audit_sleep("s-done", "2026-07-23T10:09:00.000Z", "2026-07-23T05:09:00.000Z"),
    ]
    fake = _FakeAuditClient(cycles=cycles, recoveries=recoveries, sleeps=sleeps)
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)
    monkeypatch.setattr(whoop, "fetch_workouts", lambda start_date=None: [])

    row = whoop.audit(days=7)[0]

    assert row["cycle_kind"] == "completed"
    assert row["sleep_hours"] == 8.0
    assert row["date"] == "2026-07-23"


def test_audit_ignores_a_sleep_that_did_not_open_the_cycle(monkeypatch):
    """A nap must not supply the sleep columns here either."""
    cycles = [
        {
            "id": "done", "start": "2026-07-23T05:09:00.000Z",
            "end": "2026-07-24T05:24:00.000Z", "timezone_offset": "-04:00",
            "score_state": "SCORED", "score": {"strain": 14.2},
        },
    ]
    recoveries = [
        {"cycle_id": "done", "sleep_id": "s-nap", "score_state": "SCORED",
         "score": {"recovery_score": 41}},
    ]
    sleeps = [
        # Started 15 hours after the cycle opened.
        _audit_sleep("s-nap", "2026-07-23T22:00:00.000Z", "2026-07-23T20:00:00.000Z"),
    ]
    fake = _FakeAuditClient(cycles=cycles, recoveries=recoveries, sleeps=sleeps)
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)
    monkeypatch.setattr(whoop, "fetch_workouts", lambda start_date=None: [])

    row = whoop.audit(days=7)[0]

    assert row["sleep_hours"] is None
    assert row["cycle_kind"] is None       # fetch() would skip this cycle too
    assert row["date"] == "2026-07-23"        # dated by its own start, not the nap


# --- fetch(): the current/completed split ------------------------------------------
#
# The bug being locked down: fetch() returned ONLY the most recently completed
# cycle, so the open cycle was filtered out here, inside the source, before any
# downstream code could see it. That cycle ENDED at last night's bedtime and its
# sleep is the night BEFORE last, which is why fixing brief.py changed nothing.
# Recovery and last night's sleep belong to the cycle in progress, because WHOOP
# scores recovery when the sleep that OPENS a cycle closes.

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "whoop_cycles.json"


@pytest.fixture
def payload() -> dict:
    """The saved morning, reloaded per test so one test's mutation cannot leak
    into the next."""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _http_404() -> requests.HTTPError:
    """A 404 shaped the way _get_or_none expects, for a sub-resource that does not
    exist yet (WHOOP 404s those rather than returning an empty body)."""
    response = requests.Response()
    response.status_code = 404
    return requests.HTTPError("404 not found", response=response)


class _FakeCycleClient:
    """Serves the saved fixture the way the real v2 endpoints serve it.

    Collection call for cycles, per-id lookups for recovery and sleep, and a 404
    for anything absent. No network: fetch() cannot tell this from the real client.
    """

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.closed = False

    def get_cycle_collection(self, **kwargs):
        return self.payload["cycles"]

    def get_recovery_for_cycle(self, cycle_id):
        recovery = self.payload["recovery_by_cycle"].get(str(cycle_id))
        if recovery is None:
            raise _http_404()
        return recovery

    def get_sleep_by_id(self, sleep_id):
        sleep = self.payload["sleep_by_id"].get(sleep_id)
        if sleep is None:
            raise _http_404()
        return sleep

    def get_sleep_for_cycle(self, cycle_id):
        raise _http_404()

    def close(self) -> None:
        self.closed = True


def _serve(monkeypatch, payload: dict) -> _FakeCycleClient:
    client = _FakeCycleClient(payload)
    monkeypatch.setattr(whoop, "_authorized_client", lambda: client)
    return client


OPEN_ID = 1664667247
DONE_ID = 1644738825
OLDER_ID = 1634812900   # also finished and scored, one day further back


def test_fetch_returns_both_cycles_tagged_with_cycle_kind(monkeypatch, payload):
    """The headline assertion: both cycles come back, correctly tagged.

    Returning only the completed one is the original bug, and it was invisible
    downstream because nothing downstream ever saw a second cycle to compare.
    """
    client = _serve(monkeypatch, payload)

    rows = whoop.fetch()

    assert [row["cycle_kind"] for row in rows] == [
        whoop.CYCLE_CURRENT, whoop.CYCLE_COMPLETED
    ]
    # Exactly two rows: the newest completed cycle only, not every scored cycle
    # in the lookback window.
    assert [row["cycle_id"] for row in rows] == [OPEN_ID, DONE_ID]
    assert client.closed is True


def test_current_row_holds_last_night_and_this_morning(monkeypatch, payload):
    _serve(monkeypatch, payload)

    current = whoop.by_kind(whoop.fetch(), whoop.CYCLE_CURRENT)

    assert current["date"] == "2026-07-24"           # the wake date, today
    assert current["sleep_hours"] == 7.5             # 8h in bed less 30m awake
    assert current["sleep_performance"] == 79.0
    assert current["recovery_score"] == 79.0
    assert current["hrv_ms"] == 70.1183
    assert current["resting_hr"] == 48.0


def test_fetch_completed_row_holds_yesterdays_strain(monkeypatch, payload):
    _serve(monkeypatch, payload)

    completed = whoop.by_kind(whoop.fetch(), whoop.CYCLE_COMPLETED)

    assert completed["date"] == "2026-07-23"
    assert completed["strain"] == 14.2361


def test_fetch_rows_are_exactly_one_night_apart(monkeypatch, payload):
    """The regression in one assertion. Both lines used to be read off the
    completed row, so the header showed 5.0h/41 while the app showed 7.5h/79."""
    _serve(monkeypatch, payload)
    rows = whoop.fetch()

    current = whoop.by_kind(rows, whoop.CYCLE_CURRENT)
    completed = whoop.by_kind(rows, whoop.CYCLE_COMPLETED)

    assert (current["sleep_hours"], current["recovery_score"]) == (7.5, 79.0)
    assert (completed["sleep_hours"], completed["recovery_score"]) == (5.0, 41.0)
    assert (
        date.fromisoformat(current["date"]) - date.fromisoformat(completed["date"])
    ) == timedelta(days=1)


def test_fetch_blanks_the_open_cycles_still_climbing_strain(monkeypatch, payload):
    """Day strain is not final until tonight's bedtime, and upsert_daily_metrics
    COALESCEs, so a partial number written now would stick until something
    non-null replaced it.

    The saved payload has score null on the open cycle, which is what v2 documents
    for a cycle that is not SCORED. A running score is set here rather than in the
    fixture because the blanking has to hold whichever shape WHOOP sends.
    """
    payload["cycles"][0]["score"] = {"strain": 3.2107, "kilojoule": 4123.6}
    _serve(monkeypatch, payload)

    current = whoop.by_kind(whoop.fetch(), whoop.CYCLE_CURRENT)

    assert current["strain"] is None
    # Still recoverable from the stored payload, per the raw-first convention.
    assert current["raw"]["cycle"]["score"]["strain"] == 3.2107


def test_fetch_current_strain_is_none_when_whoop_sends_no_score_at_all(
    monkeypatch, payload
):
    """The documented shape: score is null until score_state is SCORED."""
    assert payload["cycles"][0]["score"] is None
    _serve(monkeypatch, payload)

    assert whoop.by_kind(whoop.fetch(), whoop.CYCLE_CURRENT)["strain"] is None


def test_fetch_returns_only_the_completed_cycle_before_the_strap_syncs(
    monkeypatch, payload
):
    """Asleep, or awake but not synced: the open cycle has no scored recovery yet.

    --wait-for-wake polls on exactly this and keeps waiting, and the header omits
    recovery and sleep rather than falling back to the completed cycle.
    """
    del payload["recovery_by_cycle"][str(OPEN_ID)]
    _serve(monkeypatch, payload)

    rows = whoop.fetch()

    assert [row["cycle_kind"] for row in rows] == [whoop.CYCLE_COMPLETED]
    assert whoop.by_kind(rows, whoop.CYCLE_CURRENT) is None


def test_fetch_returns_only_the_current_cycle_when_none_completed_is_scored(
    monkeypatch, payload
):
    """The strap sat on the charger. The header still works; the strain line and
    TRAINING are what go missing."""
    del payload["recovery_by_cycle"][str(DONE_ID)]
    del payload["recovery_by_cycle"][str(OLDER_ID)]
    _serve(monkeypatch, payload)

    rows = whoop.fetch()

    assert [row["cycle_kind"] for row in rows] == [whoop.CYCLE_CURRENT]
    assert whoop.by_kind(rows, whoop.CYCLE_COMPLETED) is None


def test_fetch_skips_the_open_cycle_when_its_sleep_did_not_open_it(
    monkeypatch, payload
):
    """A nap reached through _sleep_for's fallback is not last night.

    Skipped rather than returned with blank sleep columns: with no sleep the row
    would date itself by the cycle START, which on a pre-midnight bedtime is the
    same date the completed row already has, and the two would merge in
    daily_metrics.
    """
    nap_id = payload["recovery_by_cycle"][str(OPEN_ID)]["sleep_id"]
    nap = payload["sleep_by_id"][nap_id]
    nap["start"] = "2026-07-24T19:00:00.000Z"   # 14h after the cycle opened
    nap["end"] = "2026-07-24T21:00:00.000Z"
    _serve(monkeypatch, payload)

    rows = whoop.fetch()

    assert [row["cycle_kind"] for row in rows] == [whoop.CYCLE_COMPLETED]


def test_fetch_does_not_depend_on_the_collection_order(monkeypatch, payload):
    """Neither selection may trust the endpoint's ordering.

    Taking the open cycle by index would return nothing here, which is
    indistinguishable from "not awake yet". Taking the completed cycle as the
    first finished one in list order is the subtler failure: with the list
    reversed it would pick the OLDER scored cycle, so the strain line would show
    a two-day-old number and TRAINING would bucket yesterday's workouts on a
    two-day-old date, silently. Two finished cycles are in the fixture so this
    test can tell the two apart.
    """
    payload["cycles"].reverse()  # oldest first
    _serve(monkeypatch, payload)

    rows = whoop.fetch()

    assert whoop.by_kind(rows, whoop.CYCLE_CURRENT)["cycle_id"] == OPEN_ID
    completed = whoop.by_kind(rows, whoop.CYCLE_COMPLETED)
    assert completed["cycle_id"] == DONE_ID
    assert completed["date"] == "2026-07-23"


def test_fetch_picks_the_newest_completed_cycle_not_merely_a_scored_one(
    monkeypatch, payload
):
    """Yesterday, not any day. The older cycle is equally finished and scored."""
    _serve(monkeypatch, payload)

    completed = whoop.by_kind(whoop.fetch(), whoop.CYCLE_COMPLETED)

    assert completed["cycle_id"] == DONE_ID
    assert completed["strain"] == 14.2361


def test_fetch_falls_back_to_an_older_cycle_when_the_newest_is_unscored(
    monkeypatch, payload
):
    """A day older is still better than no strain at all, which is why the loop
    walks past a closed cycle WHOOP never finished scoring."""
    del payload["recovery_by_cycle"][str(DONE_ID)]
    _serve(monkeypatch, payload)

    completed = whoop.by_kind(whoop.fetch(), whoop.CYCLE_COMPLETED)

    assert completed["cycle_id"] == OLDER_ID
    assert completed["date"] == "2026-07-22"


def test_fetch_returns_empty_list_when_there_are_no_cycles(monkeypatch, payload):
    payload["cycles"] = []
    _serve(monkeypatch, payload)

    assert whoop.fetch() == []


def test_by_kind_is_none_for_a_kind_this_fetch_did_not_return():
    assert whoop.by_kind([], whoop.CYCLE_CURRENT) is None
    assert whoop.by_kind([{"cycle_kind": "completed"}], whoop.CYCLE_CURRENT) is None


def test_storable_leaves_only_daily_metrics_columns(monkeypatch, payload):
    """The routing keys must not reach db.upsert_daily_metrics, which rejects any
    field that is not a column so a typo cannot look like a successful write."""
    _serve(monkeypatch, payload)
    row = whoop.by_kind(whoop.fetch(), whoop.CYCLE_CURRENT)

    storable = whoop.storable(row)

    assert "cycle_kind" not in storable and "cycle_id" not in storable
    allowed = {"date", "raw", "raw_json", "fetched_at", *db._METRIC_COLUMNS}
    assert set(storable) <= allowed
    # Everything else survives the strip.
    assert storable["recovery_score"] == 79.0
    assert storable["date"] == "2026-07-24"


# --- _metric_date: the sleep must belong to the cycle -------------------------------


def test_metric_date_ignores_a_sleep_that_did_not_open_the_cycle():
    """A nap reached through _sleep_for's cycle fallback must not date the cycle.

    The nap here ends at 02:35 the following local day. Trusting its end would push
    the cycle onto 07-17, the same off-by-one the cycle-end fallback produced.
    """
    cycle = {
        "id": 1644738825,
        "start": "2026-07-16T05:09:00.000Z",   # local 2026-07-16 01:09
        "end": "2026-07-17T06:35:00.000Z",
        "timezone_offset": "-04:00",
    }
    nap = {
        "start": "2026-07-17T02:00:00.000Z",   # local 07-16 22:00, ~21h after onset
        "end": "2026-07-17T06:35:00.000Z",     # local 07-17 02:35
        "timezone_offset": "-04:00",
    }
    assert whoop._metric_date(cycle, nap) == "2026-07-16"


def test_metric_date_uses_sleep_end_when_the_sleep_opened_the_cycle():
    """The normal night. Cycle start is a bedtime, sleep end is the wake instant,
    and a pre-midnight bedtime means they fall on different calendar days."""
    cycle = {
        "start": "2026-07-24T03:00:00.000Z",   # local 2026-07-23 23:00
        "end": None,
        "timezone_offset": "-04:00",
    }
    sleep = {
        "start": "2026-07-24T03:00:00.000Z",   # opens the cycle
        "end": "2026-07-24T11:00:00.000Z",     # local 07:00 on 07-24
        "timezone_offset": "-04:00",
    }
    assert whoop._metric_date(cycle, sleep) == "2026-07-24"


def test_row_drops_a_non_opening_sleeps_numbers_not_only_its_date():
    """Rejecting a nap for dating but still printing its hours is the worse bug.

    A wrong date is visible next to the WHOOP app; "Sleep 1h00m (14%)" under a
    correct date reads as a real terrible night.
    """
    cycle = {
        "id": 1644738825,
        "start": "2026-07-16T05:09:00.000Z",
        "end": "2026-07-17T06:35:00.000Z",
        "timezone_offset": "-04:00",
        "score": {"strain": 12.0},
    }
    recovery = {"score": {"recovery_score": 79, "hrv_rmssd_milli": 70.0,
                          "resting_heart_rate": 48.0}}
    nap = {
        "start": "2026-07-17T02:00:00.000Z",
        "end": "2026-07-17T06:35:00.000Z",
        "timezone_offset": "-04:00",
        "score": {
            "stage_summary": {"total_in_bed_time_milli": 3_600_000,
                              "total_awake_time_milli": 0},
            "sleep_performance_percentage": 14,
        },
    }

    row = whoop._row(cycle, recovery, nap)

    assert row["date"] == "2026-07-16"
    assert row["sleep_hours"] is None
    assert row["sleep_performance"] is None
    # Recovery and strain are the cycle's own and stay.
    assert row["recovery_score"] == 79
    assert row["strain"] == 12.0


def test_opens_cycle_at_the_slack_boundary():
    """Pins OPENING_SLEEP_SLACK so changing it cannot pass silently."""
    cycle = {"start": "2026-07-24T05:00:00.000Z"}
    exactly_at = {"start": "2026-07-24T07:00:00.000Z"}   # +2h, the limit
    just_past = {"start": "2026-07-24T07:00:01.000Z"}    # one second over

    assert whoop.OPENING_SLEEP_SLACK == timedelta(hours=2)
    assert whoop._opens_cycle(cycle, exactly_at) is True
    assert whoop._opens_cycle(cycle, just_past) is False


def test_metric_date_trusts_sleep_end_when_the_sleep_has_no_start():
    """Unverifiable is not the same as wrong: a thin record keeps the wake date
    rather than falling back to the bedtime."""
    cycle = {
        "start": "2026-07-24T03:00:00.000Z",
        "end": None,
        "timezone_offset": "-04:00",
    }
    sleep = {"end": "2026-07-24T11:00:00.000Z", "timezone_offset": "-04:00"}
    assert whoop._metric_date(cycle, sleep) == "2026-07-24"
