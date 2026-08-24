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


def test_audit_buckets_workout_onto_the_cycle_it_started_in(monkeypatch):
    """Cycles run wake -> next sleep onset, so cycle1 is 07-23's day even though
    it ends at 01:00 on 07-24, and a workout that started at 20:00 local on
    07-23 belongs to it."""
    cycles = [
        {
            "id": "cycle2",
            "start": "2026-07-24T11:00:00.000Z",  # local 07-24 07:00
            "end": "2026-07-25T04:00:00.000Z",    # local 07-25 00:00
            "timezone_offset": "-04:00",
            "score_state": "SCORED",
            "score": {"strain": 5.0},
        },
        {
            "id": "cycle1",
            "start": "2026-07-23T11:00:00.000Z",  # local 07-23 07:00
            "end": "2026-07-24T05:00:00.000Z",    # local 07-24 01:00
            "timezone_offset": "-04:00",
            "score_state": "SCORED",
            "score": {"strain": 10.0},
        },
    ]
    recoveries = [
        {"cycle_id": "cycle1", "sleep_id": "sleep1", "score_state": "SCORED",
         "score": {"recovery_score": 60}},
        {"cycle_id": "cycle2", "sleep_id": "sleep2", "score_state": "SCORED",
         "score": {"recovery_score": 70}},
    ]
    sleeps = [
        _audit_sleep("sleep1", "2026-07-23T11:00:00.000Z"),
        _audit_sleep("sleep2", "2026-07-24T11:00:00.000Z"),
    ]
    fake = _FakeAuditClient(cycles=cycles, recoveries=recoveries, sleeps=sleeps)
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)
    monkeypatch.setattr(
        whoop, "fetch_workouts",
        lambda start_date=None: [{
            "id": "w1",
            "date": "2026-07-23",
            "sport_name": "volleyball",
            "sport_id": 34,
            "duration_min": 90,
            "strain": 12.0,
            "start_utc": "2026-07-24T00:00:00.000Z",  # local 07-23 20:00
        }],
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
    assert day1["start_local"] == "2026-07-23 07:00"
    assert day1["end_local"] == "2026-07-24 01:00"
    assert day1["workouts"] == [
        {"sport": "volleyball", "duration_min": 90, "strain": 12.0}
    ]

    # The cycle with no matching workout gets an empty list, not an omitted key
    # or None.
    day2 = by_date["2026-07-24"]
    assert day2["workouts"] == []


def test_audit_workout_after_midnight_stays_on_the_cycle_that_was_open(monkeypatch):
    """The bug a date-string join would reintroduce: a 00:30 workout carries the
    NEXT day's local date but belongs to the cycle still open at 00:30."""
    cycles = [
        {
            "id": "cycle1",
            "start": "2026-07-23T11:00:00.000Z",  # local 07-23 07:00
            "end": "2026-07-24T05:00:00.000Z",    # local 07-24 01:00
            "timezone_offset": "-04:00",
            "score_state": "SCORED",
            "score": {"strain": 10.0},
        },
    ]
    fake = _FakeAuditClient(cycles=cycles, recoveries=[], sleeps=[])
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)
    monkeypatch.setattr(
        whoop, "fetch_workouts",
        lambda start_date=None: [{
            "id": "w_late",
            "date": "2026-07-24",  # the misleading local date
            "sport_name": "weightlifting",
            "sport_id": 45,
            "duration_min": 30,
            "strain": 4.0,
            "start_utc": "2026-07-24T04:30:00.000Z",  # local 07-24 00:30
        }],
    )

    rows = whoop.audit(days=7)

    assert len(rows) == 1
    assert rows[0]["date"] == "2026-07-23"
    assert [w["sport"] for w in rows[0]["workouts"]] == ["weightlifting"]


def test_audit_oauth_error_raises_whoop_auth_error(monkeypatch):
    fake = _FakeAuditClient(raise_exc=OAuthError("invalid_grant", "token revoked"))
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)
    monkeypatch.setattr(whoop, "fetch_workouts", lambda start_date=None: [])

    with pytest.raises(whoop.WhoopAuthError):
        whoop.audit(days=7)

    assert fake.closed is True


# --- _cycle_date: label a cycle by its START, never its end ------------------------
#
# A WHOOP cycle runs wake -> next sleep onset, so `end` lands after midnight on
# any night you get to bed past 12. Labeling by end pushes that whole day of
# strain and workouts onto tomorrow's date. These pin the rule down so the
# regression cannot come back quietly.

# All fixtures below are America/New_York in August, offset -04:00.
_OFFSET = "-04:00"


def test_cycle_date_uses_start_when_the_cycle_ends_after_midnight():
    cycle = {
        "id": "c1",
        "start": "2026-08-22T11:00:00.000Z",  # local 2026-08-22 07:00
        "end": "2026-08-23T05:01:00.000Z",    # local 2026-08-23 01:01
        "timezone_offset": _OFFSET,
    }
    assert whoop._cycle_date(cycle) == "2026-08-22"


def test_cycle_date_of_an_open_cycle_is_its_start_date():
    cycle = {
        "id": "c2",
        "start": "2026-08-23T11:30:00.000Z",  # local 2026-08-23 07:30
        "end": None,
        "timezone_offset": _OFFSET,
    }
    assert whoop._cycle_date(cycle) == "2026-08-23"


def test_cycle_date_ignores_the_end_entirely():
    """Same start, wildly different ends, same assigned date."""
    early = {"id": "c3", "start": "2026-08-22T11:00:00.000Z",
             "end": "2026-08-22T23:00:00.000Z", "timezone_offset": _OFFSET}
    late = {"id": "c4", "start": "2026-08-22T11:00:00.000Z",
            "end": "2026-08-23T06:45:00.000Z", "timezone_offset": _OFFSET}
    assert whoop._cycle_date(early) == whoop._cycle_date(late) == "2026-08-22"


# --- workout-to-cycle matching -----------------------------------------------------
#
# cycle.start <= workout.start < cycle.end, on the workout's START only.

_CYCLE_YESTERDAY = {
    "id": "c_yesterday",
    "start": "2026-08-22T11:00:00.000Z",  # local 08-22 07:00
    "end": "2026-08-23T05:01:00.000Z",    # local 08-23 01:01
    "timezone_offset": _OFFSET,
    "score_state": "SCORED",
    "score": {"strain": 14.6},
}

_CYCLE_TODAY = {
    "id": "c_today",
    "start": "2026-08-23T11:30:00.000Z",  # local 08-23 07:30
    "end": None,                           # still open
    "timezone_offset": _OFFSET,
    "score_state": "PENDING_SCORE",
}


def _workout(workout_id: str, start_utc: str, end_utc: str) -> dict:
    return {
        "id": workout_id,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "date": whoop._local_date(start_utc, _OFFSET),
        "sport_name": "volleyball",
        "duration_min": 90,
        "strain": 12.0,
    }


def test_match_workouts_attributes_by_start_not_by_local_date():
    """A workout begun at 00:30 local belongs to the cycle that was still open
    at 00:30, which is the PREVIOUS day's cycle. Bucketing by the workout's own
    local date would file it under 08-23 and steal it from 08-22."""
    late_night = _workout("w_late", "2026-08-23T04:30:00.000Z", "2026-08-23T05:00:00.000Z")
    assert late_night["date"] == "2026-08-23"  # the misleading value

    matched = whoop._match_workouts_to_cycles(
        [_CYCLE_TODAY, _CYCLE_YESTERDAY], [late_night]
    )

    assert [w["id"] for w in matched["c_yesterday"]] == ["w_late"]
    assert matched["c_today"] == []


def test_match_workouts_ignores_the_workout_end():
    """A workout that STARTS inside a cycle and ENDS after it closes stays on
    the cycle it started in, rather than moving or being counted twice."""
    straddling = _workout("w_straddle", "2026-08-23T04:50:00.000Z", "2026-08-23T12:00:00.000Z")

    matched = whoop._match_workouts_to_cycles(
        [_CYCLE_TODAY, _CYCLE_YESTERDAY], [straddling]
    )

    assert [w["id"] for w in matched["c_yesterday"]] == ["w_straddle"]
    assert matched["c_today"] == []


def test_match_workouts_open_cycle_has_no_upper_bound():
    today = _workout("w_today", "2026-08-23T15:00:00.000Z", "2026-08-23T16:30:00.000Z")

    matched = whoop._match_workouts_to_cycles(
        [_CYCLE_TODAY, _CYCLE_YESTERDAY], [today]
    )

    assert [w["id"] for w in matched["c_today"]] == ["w_today"]
    assert matched["c_yesterday"] == []


def test_match_workouts_boundary_is_half_open():
    """A workout starting exactly at a cycle's end belongs to the NEXT cycle,
    so back-to-back cycles never both claim it."""
    first = {"id": "a", "start": "2026-08-22T11:00:00.000Z",
             "end": "2026-08-23T05:00:00.000Z", "timezone_offset": _OFFSET}
    second = {"id": "b", "start": "2026-08-23T05:00:00.000Z",
              "end": "2026-08-24T05:00:00.000Z", "timezone_offset": _OFFSET}
    on_the_seam = _workout("w_seam", "2026-08-23T05:00:00.000Z", "2026-08-23T06:00:00.000Z")

    matched = whoop._match_workouts_to_cycles([second, first], [on_the_seam])

    assert matched["a"] == []
    assert [w["id"] for w in matched["b"]] == ["w_seam"]


def test_match_workouts_warns_and_does_not_double_count_overlapping_cycles(caplog):
    """Overlapping cycles are a data problem, not something to average over.
    The workout lands on exactly one cycle and the overlap is logged."""
    overlapping = {"id": "c_overlap", "start": "2026-08-22T11:00:00.000Z",
                   "end": "2026-08-23T09:00:00.000Z", "timezone_offset": _OFFSET}
    both = _workout("w_both", "2026-08-23T04:30:00.000Z", "2026-08-23T05:00:00.000Z")

    with caplog.at_level("WARNING"):
        matched = whoop._match_workouts_to_cycles(
            [overlapping, _CYCLE_YESTERDAY], [both]
        )

    landed = [cid for cid, rows in matched.items() if rows]
    assert landed == ["c_overlap"]  # first match wins, counted once
    assert "w_both" in caplog.text
    assert "c_overlap" in caplog.text and "c_yesterday" in caplog.text


def test_match_workouts_every_cycle_gets_a_key_even_with_no_workouts():
    matched = whoop._match_workouts_to_cycles([_CYCLE_TODAY, _CYCLE_YESTERDAY], [])
    assert matched == {"c_today": [], "c_yesterday": []}


def test_match_workouts_startless_workout_is_skipped_with_a_warning(caplog):
    with caplog.at_level("WARNING"):
        matched = whoop._match_workouts_to_cycles(
            [_CYCLE_TODAY], [{"id": "w_broken", "start_utc": None}]
        )
    assert matched == {"c_today": []}
    assert "w_broken" in caplog.text


# --- fetch(): the current cycle AND the completed one ------------------------------
#
# The regression this pins: fetch() used to return only the most recently
# CLOSED cycle. Last night's sleep and this morning's recovery live on the cycle
# that is still OPEN, so the briefing header showed the morning before. Both
# cycles are needed, and they are tagged so callers select by meaning rather
# than by list position.

_SLEEP_LAST_NIGHT = {
    "id": "s_last_night",
    "score_state": "SCORED",
    "end": "2026-08-23T11:30:00.000Z",
    "timezone_offset": _OFFSET,
    "score": {
        "stage_summary": {
            "total_in_bed_time_milli": 27_000_000,   # 7h30m in bed
            "total_awake_time_milli": 1_800_000,     # 30m awake -> 7.0h asleep
        },
        "sleep_performance_percentage": 90,
    },
}

_SLEEP_NIGHT_BEFORE = {
    "id": "s_night_before",
    "score_state": "SCORED",
    "end": "2026-08-22T11:00:00.000Z",
    "timezone_offset": _OFFSET,
    "score": {
        "stage_summary": {
            "total_in_bed_time_milli": 18_000_000,   # 5h in bed
            "total_awake_time_milli": 0,
        },
        "sleep_performance_percentage": 55,
    },
}

_RECOVERY_TODAY = {
    "cycle_id": "c_today",
    "sleep_id": "s_last_night",
    "score_state": "SCORED",
    "score": {"recovery_score": 72, "hrv_rmssd_milli": 84.0, "resting_heart_rate": 48.0},
}

_RECOVERY_YESTERDAY = {
    "cycle_id": "c_yesterday",
    "sleep_id": "s_night_before",
    "score_state": "SCORED",
    "score": {"recovery_score": 41, "hrv_rmssd_milli": 52.0, "resting_heart_rate": 57.0},
}


def _not_found() -> requests.HTTPError:
    """The 404 WHOOP returns for a sub-resource a cycle does not have.

    _get_or_none turns exactly this into None; any other status still raises, so
    the fake has to carry a real response object with a status code on it.
    """
    response = requests.Response()
    response.status_code = 404
    return requests.HTTPError("404 not found", response=response)


class _FakeCycleClient:
    """Stands in for WhoopClient across the cycle, recovery and sleep calls.

    Sub-resource lookups are dict gets keyed the way the real endpoints are, so
    a cycle with no scored recovery, or a recovery pointing at a sleep that is
    not scored yet, is modelled by leaving the entry out.
    """

    def __init__(self, cycles, recovery_by_cycle=None, sleep_by_id=None) -> None:
        self._cycles = cycles
        self._recovery_by_cycle = recovery_by_cycle or {}
        self._sleep_by_id = sleep_by_id or {}
        self.closed = False

    def get_cycle_collection(self, **kwargs):
        return self._cycles

    def get_recovery_for_cycle(self, cycle_id):
        recovery = self._recovery_by_cycle.get(cycle_id)
        if recovery is None:
            raise _not_found()
        return recovery

    def get_sleep_by_id(self, sleep_id):
        sleep = self._sleep_by_id.get(sleep_id)
        if sleep is None:
            raise _not_found()
        return sleep

    def get_sleep_for_cycle(self, cycle_id):
        raise _not_found()

    def close(self) -> None:
        self.closed = True


def _full_client() -> _FakeCycleClient:
    """One open cycle and one completed cycle, newest first, both fully scored."""
    return _FakeCycleClient(
        cycles=[_CYCLE_TODAY, _CYCLE_YESTERDAY],
        recovery_by_cycle={
            "c_today": _RECOVERY_TODAY,
            "c_yesterday": _RECOVERY_YESTERDAY,
        },
        sleep_by_id={
            "s_last_night": _SLEEP_LAST_NIGHT,
            "s_night_before": _SLEEP_NIGHT_BEFORE,
        },
    )


def _by_kind(rows: list[dict]) -> dict:
    return {row["cycle_kind"]: row for row in rows}


def test_fetch_returns_both_cycles_tagged(monkeypatch):
    fake = _full_client()
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    by_kind = _by_kind(whoop.fetch())

    assert set(by_kind) == {"current", "completed"}
    assert by_kind["current"]["cycle_id"] == "c_today"
    assert by_kind["completed"]["cycle_id"] == "c_yesterday"
    assert fake.closed is True


def test_fetch_current_cycle_carries_last_nights_sleep_and_todays_recovery(monkeypatch):
    monkeypatch.setattr(whoop, "_authorized_client", _full_client)

    current = _by_kind(whoop.fetch())["current"]

    assert current["date"] == "2026-08-23"          # the open cycle's START date
    assert current["recovery_score"] == 72
    assert current["hrv_ms"] == 84.0
    assert current["resting_hr"] == 48.0
    assert current["sleep_hours"] == 7.0
    assert current["sleep_performance"] == 90
    # The open cycle has no final strain. It must not carry one, or the header
    # would print a day's partial total as "Yesterday's Strain".
    assert current["strain"] is None


def test_fetch_completed_cycle_carries_yesterdays_final_strain(monkeypatch):
    monkeypatch.setattr(whoop, "_authorized_client", _full_client)

    completed = _by_kind(whoop.fetch())["completed"]

    # Ends 01:01 local on 08-23 and is still 08-22's day.
    assert completed["date"] == "2026-08-22"
    assert completed["strain"] == 14.6
    assert completed["recovery_score"] == 41


def test_fetch_returns_only_completed_when_no_cycle_is_open(monkeypatch):
    fake = _FakeCycleClient(
        cycles=[_CYCLE_YESTERDAY],
        recovery_by_cycle={"c_yesterday": _RECOVERY_YESTERDAY},
        sleep_by_id={"s_night_before": _SLEEP_NIGHT_BEFORE},
    )
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    rows = whoop.fetch()

    assert [row["cycle_kind"] for row in rows] == ["completed"]
    assert rows[0]["cycle_id"] == "c_yesterday"


def test_fetch_drops_the_open_cycle_until_its_recovery_is_scored(monkeypatch):
    """Before the strap syncs after you wake the open cycle exists but has no
    scored recovery. Normal: the completed cycle still comes back on its own."""
    fake = _FakeCycleClient(
        cycles=[_CYCLE_TODAY, _CYCLE_YESTERDAY],
        recovery_by_cycle={"c_yesterday": _RECOVERY_YESTERDAY},   # none for c_today
        sleep_by_id={"s_night_before": _SLEEP_NIGHT_BEFORE},
    )
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    assert [row["cycle_kind"] for row in whoop.fetch()] == ["completed"]


def test_fetch_drops_the_open_cycle_when_its_sleep_is_not_scored(monkeypatch):
    fake = _FakeCycleClient(
        cycles=[_CYCLE_TODAY, _CYCLE_YESTERDAY],
        recovery_by_cycle={
            "c_today": _RECOVERY_TODAY,
            "c_yesterday": _RECOVERY_YESTERDAY,
        },
        sleep_by_id={"s_night_before": _SLEEP_NIGHT_BEFORE},   # s_last_night absent
    )
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    assert [row["cycle_kind"] for row in whoop.fetch()] == ["completed"]


def test_fetch_returns_nothing_when_neither_cycle_is_scored(monkeypatch):
    fake = _FakeCycleClient(cycles=[_CYCLE_TODAY, _CYCLE_YESTERDAY])
    monkeypatch.setattr(whoop, "_authorized_client", lambda: fake)

    assert whoop.fetch() == []


def test_fetch_current_and_completed_are_different_days(monkeypatch):
    """The whole point of the split: two rows, two days, two sets of numbers, so
    a caller reading the header off the wrong one is visibly wrong rather than
    quietly one night stale."""
    monkeypatch.setattr(whoop, "_authorized_client", _full_client)

    by_kind = _by_kind(whoop.fetch())

    assert by_kind["current"]["date"] != by_kind["completed"]["date"]
    assert by_kind["current"]["recovery_score"] != by_kind["completed"]["recovery_score"]
    assert by_kind["current"]["sleep_hours"] != by_kind["completed"]["sleep_hours"]


# --- db_row() ----------------------------------------------------------------------


def test_db_row_strips_the_cycle_tags_and_keeps_everything_else(monkeypatch):
    monkeypatch.setattr(whoop, "_authorized_client", _full_client)
    current = _by_kind(whoop.fetch())["current"]

    row = whoop.db_row(current)

    assert "cycle_kind" not in row and "cycle_id" not in row
    assert row["date"] == current["date"]
    assert row["recovery_score"] == current["recovery_score"]
    assert row["raw"] is current["raw"]


def test_db_row_output_is_accepted_by_upsert_daily_metrics(monkeypatch, tmp_path):
    """The contract that matters: upsert_daily_metrics rejects unknown keys, so
    a tag left on a row would raise at write time inside the 7 AM job."""
    from src import db

    monkeypatch.setattr(whoop, "_authorized_client", _full_client)
    db_path = tmp_path / "test.db"
    db.init_db(db_path)

    for row in whoop.fetch():
        db.upsert_daily_metrics(whoop.db_row(row), db_path)  # must not raise
