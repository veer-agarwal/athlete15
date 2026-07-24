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


# --- brief_exists ------------------------------------------------------------


def test_brief_exists_false_when_nothing_recorded(dbpath):
    assert db.brief_exists("2026-07-23", path=dbpath) is False


def test_brief_exists_true_after_record_brief(dbpath):
    db.record_brief("2026-07-23", "some briefing text", path=dbpath)
    assert db.brief_exists("2026-07-23", path=dbpath) is True


def test_brief_exists_false_for_a_different_date(dbpath):
    db.record_brief("2026-07-23", "some briefing text", path=dbpath)
    assert db.brief_exists("2026-07-24", path=dbpath) is False


def test_brief_exists_true_even_when_sent_at_is_none(dbpath):
    """A row exists whether or not delivery succeeded (sent_at may be NULL);
    that is what keeps a Telegram outage from triggering a resend."""
    db.record_brief("2026-07-23", "some briefing text", sent_at=None, path=dbpath)
    assert db.brief_exists("2026-07-23", path=dbpath) is True


# --- whoop_workouts_between ----------------------------------------------------


def _workout_row(id, date, start_utc, **overrides):
    row = {
        "id": id,
        "date": date,
        "sport_id": 1,
        "start_utc": start_utc,
        "end_utc": None,
        "duration_min": 60,
        "strain": 10.0,
        "average_hr": 140,
        "max_hr": 170,
        "kilojoule": 500.0,
    }
    row.update(overrides)
    return row


def test_whoop_workouts_between_no_rows_returns_empty_list(dbpath):
    assert db.whoop_workouts_between("2026-07-01", "2026-07-31", path=dbpath) == []


def test_whoop_workouts_between_orders_by_start_utc(dbpath):
    db.upsert_whoop_workout(
        _workout_row("late", "2026-07-23", "2026-07-23T20:00:00+00:00"), path=dbpath
    )
    db.upsert_whoop_workout(
        _workout_row("early", "2026-07-23", "2026-07-23T09:00:00+00:00"), path=dbpath
    )

    result = db.whoop_workouts_between("2026-07-23", "2026-07-23", path=dbpath)

    assert [row["id"] for row in result] == ["early", "late"]


def test_whoop_workouts_between_is_inclusive_of_both_endpoint_dates(dbpath):
    db.upsert_whoop_workout(
        _workout_row("start", "2026-07-01", "2026-07-01T09:00:00+00:00"), path=dbpath
    )
    db.upsert_whoop_workout(
        _workout_row("end", "2026-07-10", "2026-07-10T09:00:00+00:00"), path=dbpath
    )
    db.upsert_whoop_workout(
        _workout_row("outside", "2026-07-11", "2026-07-11T09:00:00+00:00"), path=dbpath
    )

    result = db.whoop_workouts_between("2026-07-01", "2026-07-10", path=dbpath)

    assert {row["id"] for row in result} == {"start", "end"}


def test_whoop_workouts_between_no_rows_in_range_but_others_exist(dbpath):
    db.upsert_whoop_workout(
        _workout_row("elsewhere", "2026-01-01", "2026-01-01T09:00:00+00:00"), path=dbpath
    )
    assert db.whoop_workouts_between("2026-07-23", "2026-07-23", path=dbpath) == []


def test_whoop_workouts_between_includes_sport_name(dbpath):
    db.upsert_whoop_workout(
        _workout_row(
            "w1", "2026-07-23", "2026-07-23T09:00:00+00:00", sport_name="volleyball"
        ),
        path=dbpath,
    )
    result = db.whoop_workouts_between("2026-07-23", "2026-07-23", path=dbpath)
    assert result[0]["sport_name"] == "volleyball"


# --- _migrate / init_db: whoop_workouts.sport_name -------------------------------
#
# sport_name was added to whoop_workouts after the table already existed in
# deployed databases. CREATE TABLE IF NOT EXISTS never alters an existing table,
# so the column has to be backfilled by _migrate. These confirm the migration
# runs, is idempotent, and that a fresh init_db() also ends up with the column.


def test_migrate_adds_missing_sport_name_column(tmp_path):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    try:
        # A minimal whoop_workouts table as it existed before sport_name.
        conn.execute(
            "CREATE TABLE whoop_workouts (id TEXT PRIMARY KEY, date TEXT NOT NULL)"
        )
        db._migrate(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(whoop_workouts)")}
        assert "sport_name" in cols
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    try:
        conn.execute(
            "CREATE TABLE whoop_workouts (id TEXT PRIMARY KEY, date TEXT NOT NULL)"
        )
        db._migrate(conn)
        db._migrate(conn)  # must not raise a duplicate-column error
        cols = [row["name"] for row in conn.execute("PRAGMA table_info(whoop_workouts)")]
        assert cols.count("sport_name") == 1
    finally:
        conn.close()


def test_init_db_fresh_database_has_sport_name_column(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    conn = db.connect(path)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(whoop_workouts)")}
    finally:
        conn.close()
    assert "sport_name" in cols


def test_init_db_second_call_does_not_raise(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    db.init_db(path)  # safe to call on every startup, per the docstring
