"""Unit tests for src/main.py: wake-triggered briefing helpers.

No network, no real WHOOP, no real Telegram, no real sleeping. socket.getaddrinfo
and requests.head are mocked for the network probe; time.sleep and time.monotonic
are mocked so nothing here actually waits; datetime.now(tz) is replaced with a
fixed-clock stand-in so the wait_for_wake loop is deterministic.

Run with:  pytest tests/test_main.py
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
import requests
from zoneinfo import ZoneInfo

from src import config, main


NY = ZoneInfo("America/New_York")


class _FixedDatetime:
    """Stand-in for the `datetime` name main.py imported, with `.now(tz)` pinned
    to one moment. Everything else about that moment (`.replace`, `.strftime`,
    comparisons, subtraction) is the real datetime.datetime behavior, since the
    fixed moment itself is a genuine object, not a further mock.
    """

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self, tz=None):
        return self._moment


# --- _is_main_sleep --------------------------------------------------------------


def test_is_main_sleep_true_when_well_over_threshold():
    assert main._is_main_sleep({"sleep_hours": 7.0}) is True


def test_is_main_sleep_false_at_or_under_threshold():
    assert main._is_main_sleep({"sleep_hours": main.NAP_MIN_SLEEP_HOURS}) is False
    assert main._is_main_sleep({"sleep_hours": 1.0}) is False


def test_is_main_sleep_false_when_sleep_hours_missing():
    assert main._is_main_sleep({}) is False
    assert main._is_main_sleep({"sleep_hours": None}) is False


# --- _host_reachable -------------------------------------------------------------


def test_host_reachable_true_when_dns_and_head_both_succeed(monkeypatch):
    monkeypatch.setattr(main.socket, "getaddrinfo", MagicMock(return_value=[]))
    monkeypatch.setattr(main.requests, "head", MagicMock(return_value=MagicMock()))
    assert main._host_reachable("api.prod.whoop.com") is True


def test_host_reachable_false_when_dns_fails(monkeypatch):
    monkeypatch.setattr(main.socket, "getaddrinfo", MagicMock(side_effect=OSError("no dns")))
    mock_head = MagicMock()
    monkeypatch.setattr(main.requests, "head", mock_head)
    assert main._host_reachable("api.prod.whoop.com") is False
    mock_head.assert_not_called()


def test_host_reachable_false_when_head_raises(monkeypatch):
    monkeypatch.setattr(main.socket, "getaddrinfo", MagicMock(return_value=[]))
    monkeypatch.setattr(
        main.requests, "head", MagicMock(side_effect=requests.ConnectionError("down"))
    )
    assert main._host_reachable("api.prod.whoop.com") is False


def test_host_reachable_true_on_non_2xx_response():
    """Any HTTP status counts as reachable, an error page still proves the
    connection is up, which is all this probe checks."""
    import unittest.mock as um
    with um.patch.object(main.socket, "getaddrinfo", return_value=[]), \
         um.patch.object(main.requests, "head", return_value=MagicMock(status_code=401)):
        assert main._host_reachable("api.notion.com") is True


# --- _wait_for_network -----------------------------------------------------------


def test_wait_for_network_returns_promptly_when_reachable(monkeypatch):
    monkeypatch.setattr(main, "_host_reachable", MagicMock(return_value=True))
    monkeypatch.setattr(main.time, "monotonic", MagicMock(side_effect=[0.0, 5.0]))
    mock_sleep = MagicMock()
    monkeypatch.setattr(main.time, "sleep", mock_sleep)

    elapsed = main._wait_for_network()

    assert elapsed == 5.0
    mock_sleep.assert_not_called()


def test_wait_for_network_gives_up_after_cap_without_looping_forever(monkeypatch):
    monkeypatch.setattr(main, "_host_reachable", MagicMock(return_value=False))
    # start=0.0, first elapsed check=10.0 (< cap, one sleep), second=200.0 (>= cap, stop)
    monkeypatch.setattr(main.time, "monotonic", MagicMock(side_effect=[0.0, 10.0, 200.0]))
    mock_sleep = MagicMock()
    monkeypatch.setattr(main.time, "sleep", mock_sleep)

    elapsed = main._wait_for_network()

    assert elapsed == 200.0
    mock_sleep.assert_called_once_with(main.NETWORK_PROBE_INTERVAL_SECONDS)


def _current(fields: dict) -> dict:
    """A fetch() row tagged as the cycle in progress.

    wait_for_wake selects on cycle_kind, never on list position, so every fake
    row has to carry the tag.
    """
    return {"cycle_kind": main.whoop.CYCLE_CURRENT, "cycle_id": 1664667247, **fields}


# --- wait_for_wake -----------------------------------------------------------------


def test_wait_for_wake_idempotent_when_brief_already_recorded(monkeypatch):
    monkeypatch.setattr(main.db, "brief_exists", MagicMock(return_value=True))
    mock_wait_net = MagicMock()
    monkeypatch.setattr(main, "_wait_for_network", mock_wait_net)
    mock_deliver = MagicMock()
    monkeypatch.setattr(main, "_deliver", mock_deliver)
    mock_fetch = MagicMock()
    monkeypatch.setattr(main.whoop, "fetch", mock_fetch)

    main.wait_for_wake()

    mock_deliver.assert_not_called()
    mock_wait_net.assert_not_called()
    mock_fetch.assert_not_called()


def test_wait_for_wake_ignores_a_completed_cycle_with_a_full_nights_sleep(monkeypatch):
    """The wake trigger keys on the CURRENT cycle only.

    The most recently completed cycle closes at bedtime and is scored while you
    are still asleep, and it carries a full night of sleep: the night before last.
    Delivering on it fired the briefing in the middle of the night with the
    previous night's numbers. Here fetch() returns only that cycle, exactly as it
    does before the strap syncs, and the loop must keep waiting.
    """
    fixed_now = datetime(2026, 7, 24, 7, 5, tzinfo=NY)
    monkeypatch.setattr(main, "datetime", _FixedDatetime(fixed_now))
    monkeypatch.setattr(main.db, "brief_exists", MagicMock(return_value=False))
    monkeypatch.setattr(main, "_wait_for_network", MagicMock(return_value=0.0))
    mock_deliver = MagicMock()
    monkeypatch.setattr(main, "_deliver", mock_deliver)
    mock_sleep = MagicMock()
    monkeypatch.setattr(main.time, "sleep", mock_sleep)
    completed_only = [{
        "cycle_kind": main.whoop.CYCLE_COMPLETED,
        "cycle_id": 1644738825,
        "sleep_hours": 7.0,
    }]
    # side_effect so the second poll ends the loop instead of spinning forever.
    monkeypatch.setattr(main.whoop, "fetch", MagicMock(side_effect=[
        completed_only,
        completed_only + [_current({"sleep_hours": 7.5})],
    ]))

    main.wait_for_wake()

    # Waited a round rather than delivering on the completed cycle.
    assert mock_sleep.call_count == 1
    mock_deliver.assert_called_once_with()


def test_wait_for_wake_delivers_immediately_on_completed_main_sleep(monkeypatch):
    fixed_now = datetime(2026, 7, 24, 7, 5, tzinfo=NY)  # well before the 11:00 cutoff
    monkeypatch.setattr(main, "datetime", _FixedDatetime(fixed_now))
    monkeypatch.setattr(main.db, "brief_exists", MagicMock(return_value=False))
    monkeypatch.setattr(main, "_wait_for_network", MagicMock(return_value=0.0))
    mock_deliver = MagicMock()
    monkeypatch.setattr(main, "_deliver", mock_deliver)
    mock_sleep = MagicMock()
    monkeypatch.setattr(main.time, "sleep", mock_sleep)
    monkeypatch.setattr(main.whoop, "fetch", MagicMock(return_value=[_current({"sleep_hours": 7.0})]))

    main.wait_for_wake()

    mock_deliver.assert_called_once_with()
    mock_sleep.assert_not_called()


def test_wait_for_wake_naps_then_delivers_on_later_main_sleep(monkeypatch):
    """A short nap (sleep_hours below NAP_MIN_SLEEP_HOURS) must not trigger
    delivery; the loop keeps polling until the real main-sleep cycle closes."""
    fixed_now = datetime(2026, 7, 24, 7, 5, tzinfo=NY)
    monkeypatch.setattr(main, "datetime", _FixedDatetime(fixed_now))
    monkeypatch.setattr(main.db, "brief_exists", MagicMock(return_value=False))
    monkeypatch.setattr(main, "_wait_for_network", MagicMock(return_value=0.0))
    mock_deliver = MagicMock()
    monkeypatch.setattr(main, "_deliver", mock_deliver)
    mock_sleep = MagicMock()
    monkeypatch.setattr(main.time, "sleep", mock_sleep)
    mock_fetch = MagicMock(side_effect=[
        [_current({"sleep_hours": 1.0})],  # nap: does not deliver
        [_current({"sleep_hours": 7.0})],  # real main sleep: delivers
    ])
    monkeypatch.setattr(main.whoop, "fetch", mock_fetch)

    main.wait_for_wake()

    assert mock_fetch.call_count == 2
    assert mock_sleep.call_count == 1  # looped exactly once before delivering
    mock_deliver.assert_called_once_with()


def test_wait_for_wake_auth_failure_delivers_once_with_note_no_retry(monkeypatch):
    fixed_now = datetime(2026, 7, 24, 7, 5, tzinfo=NY)
    monkeypatch.setattr(main, "datetime", _FixedDatetime(fixed_now))
    monkeypatch.setattr(main.db, "brief_exists", MagicMock(return_value=False))
    monkeypatch.setattr(main, "_wait_for_network", MagicMock(return_value=0.0))
    mock_deliver = MagicMock()
    monkeypatch.setattr(main, "_deliver", mock_deliver)
    mock_sleep = MagicMock()
    monkeypatch.setattr(main.time, "sleep", mock_sleep)
    monkeypatch.setattr(
        main.whoop, "fetch", MagicMock(side_effect=main.whoop.WhoopAuthError("dead token"))
    )

    main.wait_for_wake()

    mock_deliver.assert_called_once()
    _, kwargs = mock_deliver.call_args
    note = kwargs.get("note") or (mock_deliver.call_args.args[0] if mock_deliver.call_args.args else "")
    assert "auth" in note.lower()
    assert "--auth" in note
    mock_sleep.assert_not_called()  # fatal, not retried


def test_wait_for_wake_past_cutoff_with_no_completed_cycle_delivers_with_note(monkeypatch):
    fixed_now = datetime(2026, 7, 24, 11, 30, tzinfo=NY)  # already past the 11:00 cutoff
    monkeypatch.setattr(main, "datetime", _FixedDatetime(fixed_now))
    monkeypatch.setattr(main.db, "brief_exists", MagicMock(return_value=False))
    monkeypatch.setattr(main, "_wait_for_network", MagicMock(return_value=0.0))
    mock_deliver = MagicMock()
    monkeypatch.setattr(main, "_deliver", mock_deliver)
    mock_sleep = MagicMock()
    monkeypatch.setattr(main.time, "sleep", mock_sleep)
    monkeypatch.setattr(main.whoop, "fetch", MagicMock(return_value=[]))

    main.wait_for_wake()

    mock_deliver.assert_called_once_with(
        note=f"No scored sleep from last night by {main.CUTOFF_HOUR}:00, sending without recovery."
    )
    mock_sleep.assert_not_called()


# --- whoop_audit -------------------------------------------------------------------
#
# Read-only CLI table for diffing WHOOP's date bucketing against the app.
# whoop.audit() is mocked directly; these only check main.whoop_audit()'s own
# printing and exit-code logic.


def test_whoop_audit_prints_table_and_returns_zero(monkeypatch, capsys):
    rows = [
        {
            "date": "2026-07-23",
            "cycle_id": "cycle1",
            "start_local": "2026-07-22 19:00",
            "end_local": "2026-07-23 09:00",
            "strain": 10.0,
            "recovery_score": 60,
            "sleep_hours": 8.0,
            "sleep_performance": 90,
            "cycle_kind": "completed",
            "workouts": [{"sport": "volleyball", "duration_min": 90, "strain": 12.0}],
        },
        {
            "date": "2026-07-24",
            "cycle_id": "cycle2",
            "start_local": "2026-07-23 19:00",
            "end_local": "2026-07-24 09:00",
            "strain": 5.0,
            "recovery_score": 70,
            "sleep_hours": 7.0,
            "sleep_performance": 85,
            "cycle_kind": "current",
            "workouts": [],
        },
    ]
    monkeypatch.setattr(main.whoop, "audit", MagicMock(return_value=rows))

    exit_code = main.whoop_audit()
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "2026-07-23  cycle cycle1" in out
    assert "strain 10.0" in out and "recovery 60" in out and "sleep 8.0h" in out
    assert "volleyball" in out and "strain 12.0" in out
    assert "(no workouts)" in out


def test_whoop_audit_prints_the_briefing_field_mapping_per_cycle(monkeypatch, capsys):
    """One line per cycle naming which briefing fields come from it.

    This is the check that the current/completed split is wired the way it is
    documented, without sending a message and reading it on the phone.
    """
    rows = [
        {
            "date": "2026-07-24", "cycle_id": "open", "start_local": "2026-07-24 01:24",
            "end_local": "-", "strain": None, "recovery_score": 79, "sleep_hours": 7.5,
            "sleep_performance": 79, "cycle_kind": "current", "workouts": [],
        },
        {
            "date": "2026-07-23", "cycle_id": "done", "start_local": "2026-07-23 01:09",
            "end_local": "2026-07-24 01:24", "strain": 14.2, "recovery_score": 41,
            "sleep_hours": 5.0, "sleep_performance": 60, "cycle_kind": "completed",
            "workouts": [],
        },
        {
            "date": "2026-07-22", "cycle_id": "older", "start_local": "2026-07-22 00:30",
            "end_local": "2026-07-23 01:09", "strain": 9.0, "recovery_score": 55,
            "sleep_hours": 6.0, "sleep_performance": 70, "cycle_kind": None,
            "workouts": [],
        },
    ]
    monkeypatch.setattr(main.whoop, "audit", MagicMock(return_value=rows))

    assert main.whoop_audit() == 0
    lines = capsys.readouterr().out.splitlines()

    briefing = [line.strip() for line in lines if line.strip().startswith("briefing:")]
    assert len(briefing) == 3
    assert "recovery, sleep, HRV, RHR" in briefing[0] and "header" in briefing[0]
    assert "strain" in briefing[1] and "TRAINING" in briefing[1]
    assert briefing[2] == "briefing: not used"


def test_whoop_audit_row_without_a_cycle_kind_key_still_prints(monkeypatch, capsys):
    """A row from an older audit shape must not take the table down."""
    rows = [{
        "date": "2026-07-23", "cycle_id": "c1", "start_local": "2026-07-22 19:00",
        "end_local": "2026-07-23 09:00", "strain": 10.0, "recovery_score": 60,
        "sleep_hours": 8.0, "sleep_performance": 90, "workouts": [],
    }]
    monkeypatch.setattr(main.whoop, "audit", MagicMock(return_value=rows))

    assert main.whoop_audit() == 0
    assert "briefing: not used" in capsys.readouterr().out


def test_whoop_audit_empty_rows_prints_message_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(main.whoop, "audit", MagicMock(return_value=[]))

    exit_code = main.whoop_audit()
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "no WHOOP cycles" in out


def test_whoop_audit_failure_prints_message_and_returns_one(monkeypatch, capsys):
    monkeypatch.setattr(main.whoop, "audit", MagicMock(side_effect=RuntimeError("boom")))

    exit_code = main.whoop_audit()
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "WHOOP audit failed" in out
