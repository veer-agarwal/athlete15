"""Unit tests for src/db.py: average_metrics and events_between.

Each test gets its own temp SQLite file via tmp_path, so nothing here touches
assistant.db. No network.

Run with:  pytest tests/test_db.py
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import db


@pytest.fixture
def dbpath(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


# --- average_metrics ---------------------------------------------------------------


def test_average_metrics_ignores_nulls(dbpath):
    """A day with recovery but no sleep (strap off charger) must not drag the
    sleep average down to a false number, nor count as a zero."""
    db.upsert_daily_metrics({"date": "2026-07-01", "recovery_score": 50, "sleep_hours": 6.0}, path=dbpath)
    db.upsert_daily_metrics({"date": "2026-07-02", "recovery_score": 70}, path=dbpath)  # no sleep_hours

    result = db.average_metrics("2026-07-01", "2026-07-02", path=dbpath)

    assert result["recovery_score"] == 60.0
    assert result["sleep_hours"] == 6.0


def test_average_metrics_empty_window_returns_nones(dbpath):
    result = db.average_metrics("2020-01-01", "2020-01-31", path=dbpath)
    assert result == {"recovery_score": None, "sleep_hours": None}


def test_average_metrics_all_null_column_returns_none(dbpath):
    """Every row present but the column itself never set anywhere in the window."""
    db.upsert_daily_metrics({"date": "2026-07-01", "recovery_score": 50}, path=dbpath)
    db.upsert_daily_metrics({"date": "2026-07-02", "recovery_score": 60}, path=dbpath)

    result = db.average_metrics("2026-07-01", "2026-07-02", path=dbpath)

    assert result["recovery_score"] == 55.0
    assert result["sleep_hours"] is None


def test_average_metrics_is_inclusive_of_both_endpoints(dbpath):
    db.upsert_daily_metrics({"date": "2026-07-01", "recovery_score": 10}, path=dbpath)
    db.upsert_daily_metrics({"date": "2026-07-10", "recovery_score": 90}, path=dbpath)
    db.upsert_daily_metrics({"date": "2026-07-11", "recovery_score": 999}, path=dbpath)  # outside

    result = db.average_metrics("2026-07-01", "2026-07-10", path=dbpath)

    assert result["recovery_score"] == 50.0


# --- events_between ------------------------------------------------------------


def _insert_event(path, uid, start_utc, summary="Event"):
    conn = db.connect(path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO events (uid, summary, location, start_utc, end_utc, source,
                                     raw_json, fetched_at)
                VALUES (?, ?, NULL, ?, NULL, 'test', NULL, ?)
                """,
                (uid, summary, start_utc, datetime.now(timezone.utc).isoformat()),
            )
    finally:
        conn.close()


def test_events_between_is_half_open_on_the_end(dbpath):
    """An event starting exactly at the end boundary belongs to the next window,
    not this one."""
    _insert_event(dbpath, "a", "2026-07-23T00:00:00+00:00")
    _insert_event(dbpath, "b", "2026-07-24T00:00:00+00:00")  # exactly at end

    result = db.events_between(
        "2026-07-23T00:00:00+00:00", "2026-07-24T00:00:00+00:00", path=dbpath
    )

    assert [e["uid"] for e in result] == ["a"]


def test_events_between_includes_the_start_boundary(dbpath):
    _insert_event(dbpath, "a", "2026-07-23T00:00:00+00:00")

    result = db.events_between(
        "2026-07-23T00:00:00+00:00", "2026-07-24T00:00:00+00:00", path=dbpath
    )

    assert len(result) == 1
    assert result[0]["uid"] == "a"


def test_events_between_orders_by_start_time(dbpath):
    _insert_event(dbpath, "late", "2026-07-23T20:00:00+00:00", summary="Late")
    _insert_event(dbpath, "early", "2026-07-23T09:00:00+00:00", summary="Early")
    _insert_event(dbpath, "mid", "2026-07-23T14:00:00+00:00", summary="Mid")

    result = db.events_between(
        "2026-07-23T00:00:00+00:00", "2026-07-24T00:00:00+00:00", path=dbpath
    )

    assert [e["summary"] for e in result] == ["Early", "Mid", "Late"]


def test_events_between_empty_when_nothing_in_range(dbpath):
    _insert_event(dbpath, "a", "2026-01-01T00:00:00+00:00")

    result = db.events_between(
        "2026-07-23T00:00:00+00:00", "2026-07-24T00:00:00+00:00", path=dbpath
    )

    assert result == []


def test_events_between_no_events_at_all(dbpath):
    assert db.events_between(
        "2026-07-23T00:00:00+00:00", "2026-07-24T00:00:00+00:00", path=dbpath
    ) == []
