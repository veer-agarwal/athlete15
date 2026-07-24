"""Unit tests for src/brief.py: formatting primitives and section blocks.

No network, no real database, no Ollama. Every block function that reaches an
external source (db, training, notion, llm) has that call mocked.

Run with:  pytest tests/test_brief.py
"""

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src import brief, config


def _today() -> date:
    """Same "today" brief.py itself uses, so date-relative tests do not need
    to freeze the clock."""
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


# --- _fmt_duration ---------------------------------------------------------------


def test_fmt_duration_typical():
    assert brief._fmt_duration(6.2) == "6h12m"


def test_fmt_duration_whole_hour_pads_minutes():
    assert brief._fmt_duration(7.0) == "7h00m"


def test_fmt_duration_rounds_to_nearest_minute():
    assert brief._fmt_duration(6.08) == "6h05m"


def test_fmt_duration_under_an_hour():
    assert brief._fmt_duration(0.5) == "0h30m"


# --- _fmt_time ---------------------------------------------------------------------


def test_fmt_time_afternoon():
    assert brief._fmt_time(datetime(2026, 7, 23, 15, 0)) == "3:00p"


def test_fmt_time_midnight_hour_is_twelve_am():
    assert brief._fmt_time(datetime(2026, 7, 23, 0, 5)) == "12:05a"


def test_fmt_time_noon_is_twelve_pm():
    assert brief._fmt_time(datetime(2026, 7, 23, 12, 30)) == "12:30p"


def test_fmt_time_single_digit_hour_no_leading_zero():
    assert brief._fmt_time(datetime(2026, 7, 23, 9, 5)) == "9:05a"


# --- _fmt_date ---------------------------------------------------------------------


def test_fmt_date_two_digit_day():
    assert brief._fmt_date(date(2026, 7, 25)) == "Jul 25"


def test_fmt_date_single_digit_day_is_width_padded():
    assert brief._fmt_date(date(2026, 7, 5)) == "Jul  5"


# --- _session_desc -----------------------------------------------------------------


def test_session_desc_full():
    session = {"type": "court", "duration_min": 90, "rpe": 6}
    assert brief._session_desc(session) == "court 90min RPE 6"


def test_session_desc_missing_duration():
    session = {"type": "court", "duration_min": None, "rpe": 6}
    assert brief._session_desc(session) == "court RPE 6"


def test_session_desc_missing_rpe():
    session = {"type": "court", "duration_min": 90, "rpe": None}
    assert brief._session_desc(session) == "court 90min"


def test_session_desc_only_type():
    session = {"type": "court", "duration_min": None, "rpe": None}
    assert brief._session_desc(session) == "court"


# --- _metrics_line -------------------------------------------------------------


def test_metrics_line_full():
    metrics = {
        "recovery_score": 54,
        "sleep_hours": 6.2,
        "sleep_performance": 71,
        "hrv_ms": 62,
        "resting_hr": 51,
    }
    assert brief._metrics_line(metrics) == (
        "Recovery 54  |  Sleep 6h12m (71%)  |  HRV 62  |  RHR 51"
    )


def test_metrics_line_missing_fields_are_omitted():
    metrics = {"recovery_score": 54, "sleep_hours": None, "hrv_ms": None, "resting_hr": None}
    assert brief._metrics_line(metrics) == "Recovery 54"


def test_metrics_line_sleep_without_performance_has_no_parens():
    metrics = {"recovery_score": None, "sleep_hours": 6.2, "sleep_performance": None,
               "hrv_ms": None, "resting_hr": None}
    assert brief._metrics_line(metrics) == "Sleep 6h12m"


def test_metrics_line_all_missing_is_empty():
    metrics = {"recovery_score": None, "sleep_hours": None, "hrv_ms": None, "resting_hr": None}
    assert brief._metrics_line(metrics) == ""


def test_metrics_line_empty_dict_is_empty():
    assert brief._metrics_line({}) == ""


# --- _sleep_note -----------------------------------------------------------------


def test_sleep_note_empty_when_sleep_hours_missing():
    metrics = {"date": "2026-07-23", "sleep_hours": None, "recovery_score": 54}
    assert brief._sleep_note(metrics) == ""


def test_sleep_note_empty_when_recovery_missing():
    metrics = {"date": "2026-07-23", "sleep_hours": 6.2, "recovery_score": None}
    assert brief._sleep_note(metrics) == ""


@patch("src.brief.db.average_metrics")
def test_sleep_note_empty_when_no_baseline_history(mock_avg):
    mock_avg.return_value = {"recovery_score": None, "sleep_hours": None}
    metrics = {"date": "2026-07-23", "sleep_hours": 6.2, "recovery_score": 54}
    assert brief._sleep_note(metrics) == ""


@patch("src.brief.db.average_metrics")
def test_sleep_note_empty_when_only_one_baseline_field_present(mock_avg):
    mock_avg.return_value = {"recovery_score": 60.0, "sleep_hours": None}
    metrics = {"date": "2026-07-23", "sleep_hours": 6.2, "recovery_score": 54}
    assert brief._sleep_note(metrics) == ""


@patch("src.brief.llm.generate_sleep_note")
@patch("src.brief.db.average_metrics")
def test_sleep_note_calls_generate_with_metric_and_baseline_values(mock_avg, mock_generate):
    mock_avg.return_value = {"recovery_score": 60.0, "sleep_hours": 6.5}
    mock_generate.return_value = "Recovery was slightly below your recent average."
    metrics = {"date": "2026-07-23", "sleep_hours": 6.2, "recovery_score": 54}

    note = brief._sleep_note(metrics)

    mock_generate.assert_called_once_with(
        sleep_hours=6.2, recovery=54.0, avg_sleep_hours=6.5, avg_recovery=60.0
    )
    assert note == "Recovery was slightly below your recent average."


@patch("src.brief.llm.generate_sleep_note", return_value=None)
@patch("src.brief.db.average_metrics")
def test_sleep_note_empty_when_llm_returns_none(mock_avg, mock_generate):
    mock_avg.return_value = {"recovery_score": 60.0, "sleep_hours": 6.5}
    metrics = {"date": "2026-07-23", "sleep_hours": 6.2, "recovery_score": 54}
    assert brief._sleep_note(metrics) == ""


@patch("src.brief.llm.generate_sleep_note", return_value="ok")
@patch("src.brief.db.average_metrics")
def test_sleep_note_baseline_window_ends_day_before_and_spans_14_days(mock_avg, mock_generate):
    mock_avg.return_value = {"recovery_score": 60.0, "sleep_hours": 6.5}
    metrics = {"date": "2026-07-23", "sleep_hours": 6.2, "recovery_score": 54}

    brief._sleep_note(metrics)

    args, _ = mock_avg.call_args
    start, end = args[0], args[1]
    assert end == "2026-07-22"
    assert start == "2026-07-09"
    span_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    assert span_days == brief.BASELINE_DAYS


@patch("src.brief.db.average_metrics", side_effect=RuntimeError("db locked"))
def test_sleep_note_empty_when_baseline_query_raises(mock_avg):
    metrics = {"date": "2026-07-23", "sleep_hours": 6.2, "recovery_score": 54}
    assert brief._sleep_note(metrics) == ""


# --- _due_block --------------------------------------------------------------------


@patch("src.brief.db.upsert_task")
@patch("src.brief.notion.fetch")
def test_due_block_renders_header_and_lines(mock_fetch, mock_upsert):
    mock_fetch.return_value = [
        {"id": "1", "title": "Econ pset 3", "due_date": "2026-07-25"},
    ]
    result = brief._due_block()
    lines = result.split("\n")
    assert lines[0] == "DUE"
    assert lines[1] == "  Jul 25  Econ pset 3"


@patch("src.brief.db.upsert_task")
@patch("src.brief.notion.fetch")
def test_due_block_marks_past_dates_overdue(mock_fetch, mock_upsert):
    past = (_today() - timedelta(days=2)).isoformat()
    mock_fetch.return_value = [{"id": "1", "title": "Lab report", "due_date": past}]
    result = brief._due_block()
    assert "(overdue)" in result
    assert f"{brief._fmt_date(date.fromisoformat(past))}  Lab report (overdue)" in result


@patch("src.brief.db.upsert_task")
@patch("src.brief.notion.fetch")
def test_due_block_future_date_has_no_overdue_suffix(mock_fetch, mock_upsert):
    future = (_today() + timedelta(days=5)).isoformat()
    mock_fetch.return_value = [{"id": "1", "title": "Quiz", "due_date": future}]
    result = brief._due_block()
    assert "(overdue)" not in result


@patch("src.brief.db.upsert_task")
@patch("src.brief.notion.fetch")
def test_due_block_undated_task(mock_fetch, mock_upsert):
    mock_fetch.return_value = [{"id": "1", "title": "Reading", "due_date": None}]
    result = brief._due_block()
    assert "  no date  Reading" in result


@patch("src.brief.db.upsert_task")
@patch("src.brief.notion.fetch")
def test_due_block_caps_at_max_items_and_reports_remainder(mock_fetch, mock_upsert):
    items = [
        {"id": str(i), "title": f"Task {i}", "due_date": None}
        for i in range(brief.MAX_DUE_ITEMS + 3)
    ]
    mock_fetch.return_value = items
    result = brief._due_block()
    lines = result.split("\n")
    # header + MAX_DUE_ITEMS lines + one "+N more" line
    assert len(lines) == 1 + brief.MAX_DUE_ITEMS + 1
    assert lines[-1] == "  +3 more"


@patch("src.brief.db.upsert_task")
@patch("src.brief.notion.fetch")
def test_due_block_untitled_task_falls_back(mock_fetch, mock_upsert):
    mock_fetch.return_value = [{"id": "1", "title": None, "due_date": None}]
    result = brief._due_block()
    assert "(untitled)" in result


@patch("src.brief.notion.fetch", side_effect=RuntimeError("notion unreachable"))
def test_due_block_empty_when_notion_raises(mock_fetch):
    assert brief._due_block() == ""


@patch("src.brief.db.upsert_task")
@patch("src.brief.notion.fetch", return_value=[])
def test_due_block_empty_when_no_items(mock_fetch, mock_upsert):
    assert brief._due_block() == ""


@patch("src.brief.db.upsert_task", side_effect=RuntimeError("db locked"))
@patch("src.brief.notion.fetch")
def test_due_block_survives_storage_failure(mock_fetch, mock_upsert):
    """A storage failure must not cost the section the data it already fetched."""
    mock_fetch.return_value = [{"id": "1", "title": "Pset", "due_date": "2026-07-25"}]
    result = brief._due_block()
    assert "Pset" in result


# --- _training_block ---------------------------------------------------------------


@patch("src.brief.training.active_injuries", return_value=[])
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_empty_when_nothing_at_all(mock_sessions, mock_load, mock_injuries):
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    assert brief._training_block() == ""


@patch("src.brief.training.active_injuries")
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_nothing_logged_line_when_injuries_present(
    mock_sessions, mock_load, mock_injuries
):
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    mock_injuries.return_value = [{
        "body_part": "shoulder", "side": None, "latest_pain": None,
        "onset_date": _today().isoformat(),
    }]
    result = brief._training_block()
    assert "  Yesterday: nothing logged" in result


@patch("src.brief.training.active_injuries", return_value=[])
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.training.sessions_between")
def test_training_block_renders_session_and_load(mock_sessions, mock_load, mock_injuries):
    mock_sessions.return_value = [{"type": "court", "duration_min": 90, "rpe": 6}]
    mock_load.return_value = {"acute": 1840.0, "chronic": 1610.0, "ratio": 1.14}
    result = brief._training_block()
    lines = result.split("\n")
    assert lines[0] == "TRAINING"
    assert lines[1] == "  Yesterday: court 90min RPE 6"
    assert lines[2] == "  7d load 1840, 28d avg 1610"


@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_injury_line_shape(mock_sessions, mock_load):
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    onset = (_today() - timedelta(days=11)).isoformat()
    with patch(
        "src.brief.training.active_injuries",
        return_value=[{
            "body_part": "shoulder", "side": "right", "latest_pain": 3,
            "onset_date": onset,
        }],
    ):
        result = brief._training_block()
    assert "  Right shoulder, 3/10, day 12" in result


@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_injury_with_no_pain_logged(mock_sessions, mock_load):
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    onset = _today().isoformat()
    with patch(
        "src.brief.training.active_injuries",
        return_value=[{
            "body_part": "ankle", "side": None, "latest_pain": None,
            "onset_date": onset,
        }],
    ):
        result = brief._training_block()
    assert "Ankle, no pain logged, day 1" in result


@patch("src.brief.training.sessions_between", side_effect=RuntimeError("db locked"))
def test_training_block_empty_when_query_raises(mock_sessions):
    assert brief._training_block() == ""


# --- _today_block --------------------------------------------------------------
#
# _today_block fetches through calendar.fetch(), wrapped in _fetch_with_retry with
# calendar.TRANSIENT_ERRORS as the retry set. Mocking src.brief.calendar.fetch
# directly is enough for every case below: the success path only calls fetch()
# once, and a raised exception that is not a member of TRANSIENT_ERRORS propagates
# out of _fetch_with_retry on the first attempt with no retry sleep involved.


@patch("src.brief.calendar.fetch", side_effect=RuntimeError("calendar unreachable"))
def test_today_block_calendar_unavailable_when_fetch_raises(mock_fetch):
    assert brief._today_block() == "TODAY\n  calendar unavailable"


@patch("src.brief.calendar.fetch", return_value=[])
def test_today_block_empty_when_no_events(mock_fetch):
    assert brief._today_block() == ""


@patch("src.brief.calendar.fetch")
def test_today_block_renders_timed_event_line(mock_fetch):
    # 19:00 UTC is 3:00pm America/New_York in July (EDT, UTC-4).
    mock_fetch.return_value = [{
        "summary": "Lift", "location": "facility",
        "start_utc": "2026-07-24T19:00:00Z", "all_day": False,
    }]
    result = brief._today_block()
    lines = result.split("\n")
    assert lines[0] == "TODAY"
    assert lines[1] == "  3:00p  Lift - facility"


@patch("src.brief.calendar.fetch")
def test_today_block_renders_all_day_event_line(mock_fetch):
    mock_fetch.return_value = [{
        "summary": "Away tournament", "location": None,
        "start_utc": "2026-07-24", "all_day": True,
    }]
    result = brief._today_block()
    assert "  all day  Away tournament" in result


@patch("src.brief.calendar.fetch")
def test_today_block_event_without_location_has_no_dash_suffix(mock_fetch):
    mock_fetch.return_value = [{
        "summary": "Class", "location": None,
        "start_utc": "2026-07-24T19:00:00Z", "all_day": False,
    }]
    result = brief._today_block()
    assert "  3:00p  Class" in result
    assert " - " not in result


# --- build() ---------------------------------------------------------------------


@patch("src.brief._fetch_metrics", return_value=None)
@patch("src.brief._news_block", return_value="NEWS\n  - headline one")
@patch("src.brief._weather_block", return_value="72F, high 84, partly cloudy, 20% precip")
@patch("src.brief._training_block", return_value="")
@patch("src.brief._due_block", return_value="")
@patch("src.brief._today_block", return_value="")
def test_build_joins_nonempty_blocks_with_blank_lines(
    mock_today, mock_due, mock_training, mock_weather, mock_news, mock_metrics
):
    result = brief.build()
    assert result == (
        "72F, high 84, partly cloudy, 20% precip\n\nNEWS\n  - headline one"
    )


@patch("src.brief._fetch_metrics", return_value=None)
@patch("src.brief._news_block", return_value="")
@patch("src.brief._weather_block", return_value="")
@patch("src.brief._training_block", return_value="")
@patch("src.brief._due_block", return_value="")
@patch("src.brief._today_block", return_value="")
def test_build_reports_no_data_when_everything_is_empty(
    mock_today, mock_due, mock_training, mock_weather, mock_news, mock_metrics
):
    assert brief.build() == "no data available this morning"
