"""Unit tests for src/brief.py: formatting primitives and section blocks.

No network, no real database, no Ollama. Every block function that reaches an
external source (db, training, notion, llm) has that call mocked.

Run with:  pytest tests/test_brief.py
"""

import json
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


# --- _fetch_current / _fetch_metrics persistence ---------------------------------


@patch("src.brief.db.upsert_daily_metrics")
@patch("src.brief.whoop.fetch_current")
def test_fetch_current_stores_the_row_with_a_null_strain(mock_fetch, mock_upsert):
    """The whole justification for blanking strain on the open cycle.

    upsert_daily_metrics COALESCEs with non-null winning, so a strain written here
    would sit in daily_metrics until something replaced it. Passing None leaves
    whatever is already stored alone, and tomorrow's completed-cycle run writes the
    final number for this date.
    """
    row = {"date": "2026-07-24", "recovery_score": 79, "sleep_hours": 7.5,
           "strain": None}
    mock_fetch.return_value = [row]

    assert brief._fetch_current() == row
    mock_upsert.assert_called_once_with(row)
    assert mock_upsert.call_args.args[0]["strain"] is None


@patch("src.brief.db.upsert_daily_metrics")
@patch("src.brief.whoop.fetch_current", return_value=[])
def test_fetch_current_stores_nothing_when_nothing_is_scored(mock_fetch, mock_upsert):
    assert brief._fetch_current() is None
    mock_upsert.assert_not_called()


@patch("src.brief.db.upsert_daily_metrics", side_effect=RuntimeError("database locked"))
@patch("src.brief.whoop.fetch_current")
def test_fetch_current_survives_a_storage_failure(mock_fetch, mock_upsert):
    """The numbers are already in hand; a locked database must not cost the
    message its header."""
    row = {"date": "2026-07-24", "recovery_score": 79}
    mock_fetch.return_value = [row]

    assert brief._fetch_current() == row


# --- _metrics_line -------------------------------------------------------------


def test_metrics_line_full():
    current = {
        "recovery_score": 54,
        "sleep_hours": 6.2,
        "sleep_performance": 71,
        "hrv_ms": 62,
        "resting_hr": 51,
    }
    assert brief._metrics_line(current, None) == (
        "Recovery 54  |  Sleep 6h12m (71%)  |  HRV 62  |  RHR 51"
    )


def test_metrics_line_missing_fields_are_omitted():
    current = {"recovery_score": 54, "sleep_hours": None, "hrv_ms": None, "resting_hr": None}
    assert brief._metrics_line(current, None) == "Recovery 54"


def test_metrics_line_sleep_without_performance_has_no_parens():
    current = {"recovery_score": None, "sleep_hours": 6.2, "sleep_performance": None,
               "hrv_ms": None, "resting_hr": None}
    assert brief._metrics_line(current, None) == "Sleep 6h12m"


def test_metrics_line_all_missing_is_empty():
    current = {"recovery_score": None, "sleep_hours": None, "hrv_ms": None, "resting_hr": None}
    assert brief._metrics_line(current, None) == ""


def test_metrics_line_empty_dict_is_empty():
    assert brief._metrics_line({}, {}) == ""


def test_metrics_line_both_none_is_empty():
    """Both cycles unavailable. build() can still call this, since it renders the
    line whenever EITHER cycle came back."""
    assert brief._metrics_line(None, None) == ""


def test_metrics_line_with_strain_adds_second_exact_line():
    current = {"recovery_score": 54, "sleep_hours": None, "hrv_ms": None, "resting_hr": None}
    completed = {"strain": 14.6}
    result = brief._metrics_line(current, completed)
    lines = result.split("\n")
    assert len(lines) == 2
    assert lines[0] == "Recovery 54"
    assert lines[1] == "Yesterday's Strain 14.6"


def test_metrics_line_strain_none_omits_second_line():
    current = {"recovery_score": 54, "sleep_hours": None, "hrv_ms": None, "resting_hr": None}
    assert brief._metrics_line(current, {"strain": None}) == "Recovery 54"


def test_metrics_line_only_strain_present_no_recovery_line():
    assert brief._metrics_line(None, {"strain": 9.2}) == "Yesterday's Strain 9.2"


def test_metrics_line_takes_recovery_and_sleep_only_from_the_current_cycle():
    """The whole point of the split. The completed cycle carries a full set of
    recovery and sleep numbers from the night before last; none of them may reach
    the header, and its strain must still reach the strain line.
    """
    current = {"recovery_score": 79, "sleep_hours": 7.5, "sleep_performance": 88,
               "hrv_ms": 70, "resting_hr": 48}
    completed = {"recovery_score": 41, "sleep_hours": 5.0, "sleep_performance": 60,
                 "hrv_ms": 55, "resting_hr": 57, "strain": 14.2}

    result = brief._metrics_line(current, completed)

    assert result == (
        "Recovery 79  |  Sleep 7h30m (88%)  |  HRV 70  |  RHR 48\n"
        "Yesterday's Strain 14.2"
    )


def test_metrics_line_omits_header_rather_than_falling_back_to_completed():
    """No current cycle yet (strap not synced) prints no recovery or sleep at all.

    Falling back to the completed cycle here is exactly the stale-by-one-night bug:
    it would print the night before last under a header stamped with today.
    """
    completed = {"recovery_score": 41, "sleep_hours": 5.0, "strain": 14.2}
    assert brief._metrics_line(None, completed) == "Yesterday's Strain 14.2"


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


@patch("src.brief.db.whoop_workouts_between", return_value=[])
@patch("src.brief.training.active_injuries", return_value=[])
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_empty_when_nothing_at_all(
    mock_sessions, mock_load, mock_injuries, mock_workouts
):
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    assert brief._training_block() == ""


@patch("src.brief.db.whoop_workouts_between", return_value=[])
@patch("src.brief.training.active_injuries")
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_nothing_logged_line_when_injuries_present(
    mock_sessions, mock_load, mock_injuries, mock_workouts
):
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    mock_injuries.return_value = [{
        "body_part": "shoulder", "side": None, "latest_pain": None,
        "onset_date": _today().isoformat(),
    }]
    result = brief._training_block()
    assert "  Yesterday: nothing logged" in result


@patch("src.brief.db.whoop_workouts_between", return_value=[])
@patch("src.brief.training.active_injuries", return_value=[])
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.training.sessions_between")
def test_training_block_renders_session_and_load(
    mock_sessions, mock_load, mock_injuries, mock_workouts
):
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


# --- _training_block: WHOOP-measured workouts ---------------------------------
#
# _training_block also calls db.whoop_workouts_between(yesterday, yesterday) and
# folds the result into the single "Yesterday: ..." line built by
# brief._yesterday_line (see the dedicated tests for that function below), rather
# than rendering a separate WHOOP list. These tests mock that call directly
# rather than hitting the real database.


@patch("src.brief.training.active_injuries", return_value=[])
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.db.whoop_workouts_between")
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_workouts_present_suppresses_nothing_logged(
    mock_sessions, mock_workouts, mock_load, mock_injuries
):
    mock_workouts.return_value = [
        {"duration_min": 88, "strain": 12.3, "sport_name": "lifting"},
    ]
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    result = brief._training_block()
    assert "nothing logged" not in result
    assert "  Yesterday: Lifting 1h28m, strain 12.3, RPE not logged" in result


@patch("src.brief.training.active_injuries", return_value=[])
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.db.whoop_workouts_between")
@patch("src.brief.training.sessions_between")
def test_training_block_manual_session_and_whoop_workout_both_shown(
    mock_sessions, mock_workouts, mock_load, mock_injuries
):
    """One "Yesterday:" line, combining the WHOOP-measured workout with the
    hand-logged RPE for the same date, rather than two separate lines."""
    mock_sessions.return_value = [{"type": "court", "duration_min": 90, "rpe": 6}]
    mock_workouts.return_value = [
        {"duration_min": 88, "strain": 12.3, "sport_name": "volleyball"},
    ]
    mock_load.return_value = {"acute": 1840.0, "chronic": 1610.0, "ratio": 1.14}
    result = brief._training_block()
    lines = result.split("\n")
    assert lines[0] == "TRAINING"
    assert lines[1] == "  Yesterday: Volleyball 1h28m, strain 12.3, RPE 6"
    assert lines[2] == "  7d load 1840, 28d avg 1610"


@patch("src.brief.training.active_injuries", return_value=[])
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.db.whoop_workouts_between")
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_multiple_workouts_shows_longest_plus_others_count(
    mock_sessions, mock_workouts, mock_load, mock_injuries
):
    """The longest-by-duration workout leads the line; the rest are summarized
    as a count rather than dropped or each getting their own line."""
    mock_workouts.return_value = [
        {"duration_min": 20, "strain": 4.0, "sport_name": "mobility"},
        {"duration_min": 60, "strain": 13.5, "sport_name": "volleyball"},
        {"duration_min": 30, "strain": 6.9, "sport_name": "lifting"},
    ]
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    result = brief._training_block()
    assert (
        "  Yesterday: Volleyball 1h00m, strain 13.5, RPE not logged "
        "(+2 other activities)" in result
    )


@patch("src.brief.training.active_injuries", return_value=[])
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.db.whoop_workouts_between")
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_longest_workout_selected_even_when_unscored(
    mock_sessions, mock_workouts, mock_load, mock_injuries
):
    """The longest workout by duration_min leads the line even when it has no
    strain yet: selection is by duration, never by strain."""
    mock_workouts.return_value = [
        {"duration_min": 40, "strain": None, "sport_name": "mobility"},
        {"duration_min": 30, "strain": 1.0, "sport_name": "lifting"},
    ]
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    result = brief._training_block()
    assert (
        "  Yesterday: Mobility 40m, RPE not logged (+1 other activity)" in result
    )


@patch("src.brief.training.active_injuries", return_value=[])
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.db.whoop_workouts_between", return_value=[])
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_empty_when_no_manual_no_whoop_no_injuries_no_load(
    mock_sessions, mock_workouts, mock_load, mock_injuries
):
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    assert brief._training_block() == ""


@patch("src.brief.training.active_injuries", return_value=[])
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.db.whoop_workouts_between")
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_not_empty_when_only_whoop_workouts_present(
    mock_sessions, mock_workouts, mock_load, mock_injuries
):
    mock_workouts.return_value = [{"duration_min": 45, "strain": 9.1}]
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    assert brief._training_block() != ""


# --- _training_block: cycle_date wiring -----------------------------------------
#
# "Yesterday" for TRAINING is driven by the completed WHOOP cycle's own assigned
# date (cycle_date), not calendar arithmetic, except when WHOOP is unavailable and
# no cycle_date was passed. These confirm the date actually used for both queries.


@patch("src.brief.training.active_injuries", return_value=[])
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.db.whoop_workouts_between", return_value=[])
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_queries_use_the_passed_cycle_date(
    mock_sessions, mock_workouts, mock_load, mock_injuries
):
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    brief._training_block(cycle_date="2026-07-20")
    mock_sessions.assert_called_once_with("2026-07-20", "2026-07-20")
    mock_workouts.assert_called_once_with("2026-07-20", "2026-07-20")


@patch("src.brief.training.active_injuries", return_value=[])
@patch("src.brief.training.acute_chronic_ratio")
@patch("src.brief.db.whoop_workouts_between", return_value=[])
@patch("src.brief.training.sessions_between", return_value=[])
def test_training_block_falls_back_to_calendar_yesterday_when_cycle_date_none(
    mock_sessions, mock_workouts, mock_load, mock_injuries
):
    mock_load.return_value = {"acute": 0.0, "chronic": 0.0, "ratio": None}
    brief._training_block(cycle_date=None)
    expected = (_today() - timedelta(days=1)).isoformat()
    mock_sessions.assert_called_once_with(expected, expected)
    mock_workouts.assert_called_once_with(expected, expected)


# --- _yesterday_line -------------------------------------------------------------


def test_yesterday_line_selects_longest_workout_by_duration():
    workouts = [
        {"duration_min": 20, "strain": 4.0, "sport_name": "lifting"},
        {"duration_min": 90, "strain": 12.3, "sport_name": "volleyball"},
        {"duration_min": 30, "strain": 6.9, "sport_name": "running"},
    ]
    result = brief._yesterday_line(workouts, [])
    assert result.startswith("  Yesterday: Volleyball 1h30m")


def test_yesterday_line_sport_name_is_capitalized():
    workouts = [{"duration_min": 60, "strain": 5.0, "sport_name": "volleyball"}]
    result = brief._yesterday_line(workouts, [])
    assert "Volleyball" in result


def test_yesterday_line_omits_strain_part_when_longest_has_none():
    workouts = [{"duration_min": 40, "strain": None, "sport_name": "lifting"}]
    result = brief._yesterday_line(workouts, [])
    assert result == "  Yesterday: Lifting 40m, RPE not logged"
    assert "strain" not in result


def test_yesterday_line_shows_logged_rpe_from_sessions():
    workouts = [{"duration_min": 90, "strain": 12.3, "sport_name": "volleyball"}]
    sessions = [{"rpe": 5}, {"rpe": 7}]  # max of the day's sessions
    result = brief._yesterday_line(workouts, sessions)
    assert result == "  Yesterday: Volleyball 1h30m, strain 12.3, RPE 7"


def test_yesterday_line_rpe_not_logged_when_no_sessions_for_the_date():
    workouts = [{"duration_min": 90, "strain": 12.3, "sport_name": "volleyball"}]
    result = brief._yesterday_line(workouts, [])
    assert "RPE not logged" in result


def test_yesterday_line_no_others_suffix_for_a_single_workout():
    workouts = [{"duration_min": 90, "strain": 12.3, "sport_name": "volleyball"}]
    result = brief._yesterday_line(workouts, [])
    assert "other activit" not in result


def test_yesterday_line_one_other_activity_is_singular():
    workouts = [
        {"duration_min": 90, "strain": 12.3, "sport_name": "volleyball"},
        {"duration_min": 20, "strain": 4.0, "sport_name": "lifting"},
    ]
    result = brief._yesterday_line(workouts, [])
    assert result.endswith("(+1 other activity)")


def test_yesterday_line_two_others_is_plural():
    workouts = [
        {"duration_min": 90, "strain": 12.3, "sport_name": "volleyball"},
        {"duration_min": 20, "strain": 4.0, "sport_name": "lifting"},
        {"duration_min": 30, "strain": 6.9, "sport_name": "running"},
    ]
    result = brief._yesterday_line(workouts, [])
    assert result.endswith("(+2 other activities)")


def test_yesterday_line_falls_back_to_manual_session_when_no_workouts():
    sessions = [{"type": "court", "duration_min": 90, "rpe": 6}]
    assert brief._yesterday_line([], sessions) == "  Yesterday: court 90min RPE 6"


def test_yesterday_line_nothing_logged_when_neither_source_has_anything():
    assert brief._yesterday_line([], []) == "  Yesterday: nothing logged"


# --- _workout_sport --------------------------------------------------------------


def test_workout_sport_uses_stored_sport_name_column():
    assert brief._workout_sport({"sport_name": "volleyball"}) == "Volleyball"


def test_workout_sport_falls_back_to_raw_json_when_column_is_none():
    workout = {
        "sport_name": None,
        "raw_json": json.dumps({"sport_name": "weightlifting"}),
    }
    assert brief._workout_sport(workout) == "Weightlifting"


def test_workout_sport_falls_back_to_sport_id_when_no_name_anywhere():
    workout = {"sport_name": None, "raw_json": None, "sport_id": 45}
    assert brief._workout_sport(workout) == "Sport 45"


def test_workout_sport_falls_back_to_workout_when_no_id_either():
    workout = {"sport_name": None, "raw_json": None, "sport_id": None}
    assert brief._workout_sport(workout) == "Workout"


# --- _fmt_workout_dur --------------------------------------------------------------


def test_fmt_workout_dur_over_an_hour():
    assert brief._fmt_workout_dur(125) == "2h05m"


def test_fmt_workout_dur_under_an_hour_has_no_hour_part():
    assert brief._fmt_workout_dur(54) == "54m"


def test_fmt_workout_dur_exact_hour_pads_minutes():
    assert brief._fmt_workout_dur(60) == "1h00m"


def test_fmt_workout_dur_none_is_question_mark():
    assert brief._fmt_workout_dur(None) == "?"


# --- _date_rpe ---------------------------------------------------------------------


def test_date_rpe_empty_list_is_none():
    assert brief._date_rpe([]) is None


def test_date_rpe_is_the_max_among_non_null_sessions():
    sessions = [{"rpe": 5}, {"rpe": 8}, {"rpe": None}]
    assert brief._date_rpe(sessions) == 8


def test_date_rpe_preserves_a_half_point_float():
    assert brief._date_rpe([{"rpe": 7.5}]) == 7.5


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


@patch("src.brief._store_yesterday_workouts")
@patch("src.brief._fetch_current", return_value=None)
@patch("src.brief._fetch_metrics", return_value=None)
@patch("src.brief._news_block", return_value="NEWS\n  - headline one")
@patch("src.brief._weather_block", return_value="72F, high 84, partly cloudy, 20% precip")
@patch("src.brief._training_block", return_value="")
@patch("src.brief._due_block", return_value="")
@patch("src.brief._today_block", return_value="")
def test_build_joins_nonempty_blocks_with_blank_lines(
    mock_today, mock_due, mock_training, mock_weather, mock_news,
    mock_metrics, mock_current, mock_store,
):
    result = brief.build()
    assert result == (
        "72F, high 84, partly cloudy, 20% precip\n\nNEWS\n  - headline one"
    )


@patch("src.brief._store_yesterday_workouts")
@patch("src.brief._fetch_current", return_value=None)
@patch("src.brief._fetch_metrics", return_value=None)
@patch("src.brief._news_block", return_value="")
@patch("src.brief._weather_block", return_value="")
@patch("src.brief._training_block", return_value="")
@patch("src.brief._due_block", return_value="")
@patch("src.brief._today_block", return_value="")
def test_build_reports_no_data_when_everything_is_empty(
    mock_today, mock_due, mock_training, mock_weather, mock_news,
    mock_metrics, mock_current, mock_store,
):
    assert brief.build() == "no data available this morning"


@patch("src.brief._store_yesterday_workouts")
@patch("src.brief._sleep_note", return_value="")
@patch("src.brief._news_block", return_value="")
@patch("src.brief._weather_block", return_value="")
@patch("src.brief._training_block", return_value="")
@patch("src.brief._due_block", return_value="")
@patch("src.brief._today_block", return_value="")
@patch("src.brief._fetch_current", return_value={"date": "2026-07-24", "recovery_score": 79,
                                                 "sleep_hours": 7.5, "sleep_performance": 79})
@patch("src.brief._fetch_metrics", return_value={"date": "2026-07-23", "recovery_score": 41,
                                                 "sleep_hours": 5.0, "strain": 14.2})
def test_build_header_reads_current_cycle_and_strain_reads_completed(
    mock_metrics, mock_current, mock_today, mock_due, mock_training,
    mock_weather, mock_news, mock_note, mock_store,
):
    """End to end through build(): the two cycles land in the two lines they own."""
    result = brief.build()
    assert result == (
        "Recovery 79  |  Sleep 7h30m (79%)\nYesterday's Strain 14.2"
    )


@patch("src.brief._store_yesterday_workouts")
@patch("src.brief._sleep_note", return_value="")
@patch("src.brief._news_block", return_value="")
@patch("src.brief._weather_block", return_value="")
@patch("src.brief._due_block", return_value="")
@patch("src.brief._today_block", return_value="")
@patch("src.brief._training_block", return_value="")
@patch("src.brief._fetch_current", return_value={"date": "2026-07-24"})
@patch("src.brief._fetch_metrics", return_value={"date": "2026-07-23"})
def test_build_passes_the_completed_cycle_date_to_training(
    mock_metrics, mock_current, mock_training, mock_today, mock_due,
    mock_weather, mock_news, mock_note, mock_store,
):
    """TRAINING buckets on the COMPLETED cycle's date, never the current one.

    Yesterday's workouts belong to yesterday's cycle; passing the current cycle's
    date would ask for today's, which at 7 AM is empty.
    """
    brief.build()
    mock_training.assert_called_once_with("2026-07-23")
