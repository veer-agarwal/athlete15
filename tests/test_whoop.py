"""Unit tests for src/sources/whoop.py: token persistence and auth-error routing.

No network: every test either only touches the filesystem via a tmp_path token
file, or replaces _authorized_client with a fake object so the real WhoopClient
and authlib never make a request.

Run with:  pytest tests/test_whoop.py
"""

import json
from pathlib import Path

import pytest
import requests
from authlib.integrations.base_client.errors import OAuthError

from src import config
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


def _audit_sleep(sleep_id: str, end_utc: str) -> dict:
    return {
        "id": sleep_id,
        "score_state": "SCORED",
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
    sleep = {"end": "2026-07-23T11:00:00.000Z", "timezone_offset": "-04:00"}  # local 07:00
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
