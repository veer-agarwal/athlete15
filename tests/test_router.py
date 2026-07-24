"""Unit tests for src/router.py: the intent router.

Spec constraint: NO model, NO network. llm.classify/llm.chat/llm.extract_event
and calendar_source.fetch are always mocked or replaced with a fixed fake, so
classification itself (nondeterministic by design) is never exercised here.
Everything that touches SQLite runs against a temp file via a monkeypatched
src.config.DB_PATH; db.connect()/training.*/router.* all read config.DB_PATH
at call time, so patching the attribute is enough without threading a path
through every call.

Run with:  pytest tests/test_router.py
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
import requests
from zoneinfo import ZoneInfo

from src import config, db, router, training

CHAT_ID = 987654321
NY = ZoneInfo("America/New_York")


# ----------------------------------------------------------------------------------
# fixtures and small helpers


class _FixedCalCache:
    """A stand-in for router._CalendarCache with a canned .get() result."""

    def __init__(self, events: list[dict], unavailable: bool = False) -> None:
        self._events = events
        self._unavailable = unavailable

    def get(self) -> tuple[list[dict], bool]:
        return list(self._events), self._unavailable


class FakeClock:
    """An injectable monotonic clock that only advances when told to."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Temp db, fixed timezone, and fresh router module state for every test.

    Individual tests override _CAL_CACHE / _PENDING further with their own
    monkeypatch.setattr calls where they need specific behavior; monkeypatch
    undoes everything at teardown regardless of ordering.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    db.init_db(db_path)
    # Pinned regardless of what .env on the dev machine says, so "today" in
    # these tests never depends on the local environment.
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    monkeypatch.setattr(router, "_PENDING", router.PendingStore())
    monkeypatch.setattr(router, "_CAL_CACHE", _FixedCalCache([], unavailable=False))
    yield db_path


def _utc(dt_ny: datetime) -> str:
    """A local NY datetime as the Z-suffixed UTC string calendar.py produces."""
    return dt_ny.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _event(summary, start_ny, end_ny=None, location=None, all_day=False):
    return {
        "uid": f"uid-{summary}",
        "summary": summary,
        "location": location,
        "start_utc": _utc(start_ny) if not all_day else start_ny,
        "end_utc": (_utc(end_ny) if end_ny is not None else _utc(start_ny)) if not all_day else end_ny,
        "calendar_name": "test",
        "all_day": all_day,
        "raw": "",
    }


def _log_entries_rows(path):
    conn = db.connect(path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM log_entries").fetchall()]
    finally:
        conn.close()


def _sessions_rows(path):
    conn = db.connect(path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM sessions").fetchall()]
    finally:
        conn.close()


# ----------------------------------------------------------------------------------
# 1. context assembly


def test_gather_context_current_event(monkeypatch):
    now = datetime(2026, 7, 23, 15, 0, tzinfo=NY)
    events = [_event("Lift", datetime(2026, 7, 23, 14, 30, tzinfo=NY),
                      datetime(2026, 7, 23, 15, 30, tzinfo=NY), location="Palladium")]
    monkeypatch.setattr(router, "_CAL_CACHE", _FixedCalCache(events))

    context = router.gather_context(now=now)

    assert context["current_event"] is not None
    assert context["current_event"]["summary"] == "Lift"
    assert context["current_event"]["location"] == "Palladium"
    assert context["recent_events"] == []
    assert context["next_event"] is None
    assert context["calendar_unavailable"] is False


def test_gather_context_recent_event_within_window(monkeypatch):
    now = datetime(2026, 7, 23, 15, 0, tzinfo=NY)
    # ended at 1 PM, 2 hours ago: inside the 3 hour recent window
    events = [_event("Court", datetime(2026, 7, 23, 12, 0, tzinfo=NY),
                      datetime(2026, 7, 23, 13, 0, tzinfo=NY))]
    monkeypatch.setattr(router, "_CAL_CACHE", _FixedCalCache(events))

    context = router.gather_context(now=now)

    assert context["current_event"] is None
    assert len(context["recent_events"]) == 1
    assert context["recent_events"][0]["summary"] == "Court"


def test_gather_context_recent_event_outside_window_is_dropped(monkeypatch):
    now = datetime(2026, 7, 23, 15, 0, tzinfo=NY)
    # ended 4 hours ago: outside the 3 hour recent window, and not "next"
    # either since it is in the past.
    events = [_event("Old", datetime(2026, 7, 23, 10, 0, tzinfo=NY),
                      datetime(2026, 7, 23, 11, 0, tzinfo=NY))]
    monkeypatch.setattr(router, "_CAL_CACHE", _FixedCalCache(events))

    context = router.gather_context(now=now)

    assert context["recent_events"] == []
    assert context["current_event"] is None
    assert context["next_event"] is None


def test_gather_context_next_event_is_the_soonest_not_the_first_in_list(monkeypatch):
    now = datetime(2026, 7, 23, 15, 0, tzinfo=NY)
    # Later event listed first on purpose: gather_context must not just take
    # the first future row, it must take the one with the earliest start.
    events = [
        _event("Later", datetime(2026, 7, 23, 18, 0, tzinfo=NY)),
        _event("Soonest", datetime(2026, 7, 23, 16, 0, tzinfo=NY)),
    ]
    monkeypatch.setattr(router, "_CAL_CACHE", _FixedCalCache(events))

    context = router.gather_context(now=now)

    assert context["next_event"]["summary"] == "Soonest"


def test_gather_context_all_day_event_excluded_from_current(monkeypatch):
    now = datetime(2026, 7, 23, 15, 0, tzinfo=NY)
    events = [_event("Mom's birthday", "2026-07-23", "2026-07-24", all_day=True)]
    monkeypatch.setattr(router, "_CAL_CACHE", _FixedCalCache(events))

    context = router.gather_context(now=now)

    assert context["current_event"] is None
    assert context["recent_events"] == []
    assert context["next_event"] is None


def test_gather_context_unparseable_event_is_skipped_not_raised(monkeypatch):
    now = datetime(2026, 7, 23, 15, 0, tzinfo=NY)
    good = _event("Lift", datetime(2026, 7, 23, 14, 30, tzinfo=NY),
                   datetime(2026, 7, 23, 15, 30, tzinfo=NY))
    missing_start = {"summary": "Broken", "all_day": False}  # no start_utc key
    garbage_start = _event("AlsoBroken", datetime(2026, 7, 23, 14, 30, tzinfo=NY))
    garbage_start["start_utc"] = "not-a-timestamp"
    events = [missing_start, garbage_start, good]
    monkeypatch.setattr(router, "_CAL_CACHE", _FixedCalCache(events))

    context = router.gather_context(now=now)  # must not raise

    assert context["current_event"]["summary"] == "Lift"


def test_gather_context_calendar_unavailable(monkeypatch):
    monkeypatch.setattr(router, "_CAL_CACHE", _FixedCalCache([], unavailable=True))

    context = router.gather_context(now=datetime(2026, 7, 23, 15, 0, tzinfo=NY))

    assert context["calendar_unavailable"] is True
    assert context["current_event"] is None
    assert context["recent_events"] == []
    assert context["next_event"] is None


def test_gather_context_rpe_logged_today_true(monkeypatch):
    now = datetime(2026, 7, 23, 15, 0, tzinfo=NY)
    training.log_session("lift", duration_min=60, rpe=7, on_date="2026-07-23")

    context = router.gather_context(now=now)

    assert context["rpe_logged_today"] is True


def test_gather_context_rpe_logged_today_false_when_nothing_logged():
    now = datetime(2026, 7, 23, 15, 0, tzinfo=NY)

    context = router.gather_context(now=now)

    assert context["rpe_logged_today"] is False


def test_gather_context_rpe_logged_today_false_when_session_has_no_rpe():
    now = datetime(2026, 7, 23, 15, 0, tzinfo=NY)
    training.log_session("lift", duration_min=60, rpe=None, on_date="2026-07-23")

    context = router.gather_context(now=now)

    assert context["rpe_logged_today"] is False


# --- format_context ----------------------------------------------------------------


def _base_context(**overrides):
    context = {
        "now_local": "2026-07-23T15:00:00-04:00",
        "weekday": "Thursday",
        "timezone": "America/New_York",
        "current_event": None,
        "recent_events": [],
        "next_event": None,
        "rpe_logged_today": False,
        "active_injuries": [],
        "calendar_unavailable": False,
    }
    context.update(overrides)
    return context


def test_format_context_has_labeled_lines():
    context = _base_context(
        current_event={
            "summary": "Lift", "location": None,
            "start_local": "2026-07-23T14:30:00-04:00",
            "end_local": "2026-07-23T15:30:00-04:00",
        },
        rpe_logged_today=True,
        active_injuries=[
            {"body_part": "shoulder", "side": "right", "latest_pain": 4,
             "latest_pain_date": "2026-07-22"}
        ],
    )

    text = router.format_context(context)

    assert "NOW: 2026-07-23T15:00:00-04:00 (Thursday)" in text
    assert "CURRENT EVENT: Lift" in text
    assert "RPE LOGGED TODAY: yes" in text
    assert "ACTIVE INJURIES: right shoulder pain 4/10 (2026-07-22)" in text


def test_format_context_no_current_event_and_no_injuries():
    text = router.format_context(_base_context())

    assert "CURRENT EVENT: none" in text
    assert "RPE LOGGED TODAY: no" in text
    assert "ACTIVE INJURIES: none" in text


def test_format_context_calendar_unavailable_branch():
    text = router.format_context(_base_context(calendar_unavailable=True))

    assert "CALENDAR: unavailable" in text
    assert "CURRENT EVENT" not in text
    assert "NEXT EVENT" not in text


# ----------------------------------------------------------------------------------
# 2. pending state machine


def test_pending_store_put_get_roundtrip():
    clock = FakeClock(1000.0)
    store = router.PendingStore(clock)
    pending = store.open(
        CHAT_ID, question="q", kind="confirm", options=[], raw_text="raw",
        entry_id=1, intent="LOG_RPE", extracted={"rpe": 7},
    )

    got = store.get(CHAT_ID)

    assert got is pending
    assert got.question == "q"
    assert got.created == 1000.0


def test_pending_store_expires_after_ttl_and_clears():
    clock = FakeClock(1000.0)
    store = router.PendingStore(clock)
    store.open(CHAT_ID, question="q", kind="confirm", options=[], raw_text="raw",
               entry_id=1, intent="LOG_RPE", extracted={})

    clock.advance(router.PENDING_TTL_SECONDS + 1)

    assert store.get(CHAT_ID) is None
    assert CHAT_ID not in store._items  # actually cleared, not just hidden


def test_pending_store_not_yet_expired_at_ttl_boundary():
    """Strictly greater than the TTL expires; exactly at it does not."""
    clock = FakeClock(1000.0)
    store = router.PendingStore(clock)
    store.open(CHAT_ID, question="q", kind="confirm", options=[], raw_text="raw",
               entry_id=1, intent="LOG_RPE", extracted={})

    clock.advance(router.PENDING_TTL_SECONDS)

    assert store.get(CHAT_ID) is not None


def test_pending_store_put_replaces_does_not_stack():
    clock = FakeClock(1000.0)
    store = router.PendingStore(clock)
    store.open(CHAT_ID, question="first", kind="confirm", options=[], raw_text="a",
               entry_id=1, intent="LOG_RPE", extracted={})
    store.open(CHAT_ID, question="second", kind="confirm", options=[], raw_text="b",
               entry_id=2, intent="LOG_PAIN", extracted={})

    got = store.get(CHAT_ID)

    assert got.question == "second"
    assert len(store._items) == 1


# --- _interpret_answer ---------------------------------------------------------


def _pending(kind, options=()):
    return router.Pending(
        question="q", kind=kind, options=list(options), raw_text="raw",
        entry_id=1, intent="LOG_RPE", extracted={}, created=0.0,
    )


@pytest.mark.parametrize("word", ["yes", "y", "yeah", "yep", "ok", "1"])
def test_interpret_answer_confirm_yes_words(word):
    assert router._interpret_answer(_pending("confirm"), word) is True


@pytest.mark.parametrize("word", ["no", "n", "nah", "nope", "2"])
def test_interpret_answer_confirm_no_words(word):
    assert router._interpret_answer(_pending("confirm"), word) is False


@pytest.mark.parametrize("word", ["maybe", "sure thing", "later", ""])
def test_interpret_answer_confirm_anything_else_is_none(word):
    assert router._interpret_answer(_pending("confirm"), word) is None


def test_interpret_answer_choice_digit_in_range():
    pending = _pending("choice", ["session RPE", "pain score"])
    assert router._interpret_answer(pending, "1") == 0
    assert router._interpret_answer(pending, "2") == 1


@pytest.mark.parametrize("digit", ["0", "3", "99"])
def test_interpret_answer_choice_digit_out_of_range(digit):
    pending = _pending("choice", ["session RPE", "pain score"])
    assert router._interpret_answer(pending, digit) is None


def test_interpret_answer_choice_exact_label_match():
    pending = _pending("choice", ["session RPE", "pain score"])
    assert router._interpret_answer(pending, "pain score") == 1


def test_interpret_answer_choice_word_subset_match():
    pending = _pending("choice", list(router._UNCLEAR_OPTIONS))
    assert router._interpret_answer(pending, "task") == 2  # "create a task"


def test_interpret_answer_choice_ambiguous_word_matches_two_options():
    pending = _pending("choice", ["red apple", "red pepper"])
    assert router._interpret_answer(pending, "red") is None


def test_interpret_answer_choice_no_recognizable_word_is_none():
    pending = _pending("choice", ["session RPE", "pain score"])
    assert router._interpret_answer(pending, "???") is None


# --- via handle_message ---------------------------------------------------------


def test_answer_message_is_written_to_log_entries_before_resolving(monkeypatch, tmp_path):
    original_id = db.insert_log_entry("lift rpe 8")
    pending = router.Pending(
        question="Log session: lift RPE 8? (yes/no)", kind="confirm", options=[],
        raw_text="lift rpe 8", entry_id=original_id, intent="LOG_RPE",
        extracted={"session_type": "lift", "duration_min": None, "rpe": 8},
        created=router._PENDING._clock(),
    )
    router._PENDING.put(CHAT_ID, pending)
    mock_classify = MagicMock()
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    reply = router.handle_message(CHAT_ID, "yes")

    mock_classify.assert_not_called()
    assert "logged" in reply
    rows = _log_entries_rows(config.DB_PATH)
    assert any(r["raw_text"] == "yes" for r in rows)
    assert len(_sessions_rows(config.DB_PATH)) == 1


def test_non_answer_while_pending_drops_it_and_reclassifies(monkeypatch):
    pending = router.Pending(
        question="Log session: lift RPE 8? (yes/no)", kind="confirm", options=[],
        raw_text="lift rpe 8", entry_id=1, intent="LOG_RPE",
        extracted={"session_type": "lift", "duration_min": None, "rpe": 8},
        created=router._PENDING._clock(),
    )
    router._PENDING.put(CHAT_ID, pending)
    mock_classify = MagicMock(return_value={
        "intent": "QUESTION", "confidence": "high", "extracted": {},
        "reasoning": "", "tier": 1,
    })
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    router.handle_message(CHAT_ID, "totally unrelated message")

    mock_classify.assert_called_once()
    assert router._PENDING.get(CHAT_ID) is None


# ----------------------------------------------------------------------------------
# 3. expiry through handle_message


def test_expired_pending_through_handle_message_is_not_treated_as_an_answer(monkeypatch):
    clock = FakeClock(1000.0)
    store = router.PendingStore(clock)
    monkeypatch.setattr(router, "_PENDING", store)

    mock_classify = MagicMock(side_effect=[
        {"intent": "UNCLEAR", "confidence": "low", "extracted": {}, "reasoning": "", "tier": 1},
        {"intent": "QUESTION", "confidence": "high", "extracted": {}, "reasoning": "", "tier": 1},
    ])
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    router.handle_message(CHAT_ID, "some vague message")
    assert store.get(CHAT_ID) is not None  # UNCLEAR always opens a pending question

    clock.advance(router.PENDING_TTL_SECONDS + 1)

    router.handle_message(CHAT_ID, "1")

    assert mock_classify.call_count == 2  # the expired "1" fell through to classify again


# ----------------------------------------------------------------------------------
# 4. correction matching


@pytest.mark.parametrize(
    "text, expected",
    [
        ("no, that was 8 not 6", {"kind": "rpe_or_pain", "new_value": 8}),
        ("actually 7.5", {"kind": "rpe_or_pain", "new_value": 7.5}),
        (
            "wrong, that was my left shoulder",
            {"kind": "body_part", "body_part": "shoulder", "side": "left"},
        ),
        ("no left", {"kind": "body_part", "body_part": None, "side": "left"}),
        ("no idea what happened", None),
        ("that hurt", None),
    ],
)
def test_detect_correction_pure_cases(text, expected):
    assert router.detect_correction(text) == expected


def test_detect_correction_out_of_range_apply_falls_through():
    """detect_correction itself cannot produce an out-of-range value (its
    number regex only matches a single digit or literal '10'), but
    _apply_correction's guard is tested directly for the defensive branch."""
    reply = router._apply_correction(
        CHAT_ID, {"kind": "rpe_or_pain", "new_value": 15}, "no, that was 15", entry_id=1
    )
    assert reply is None


def test_correction_amends_todays_session_rpe_no_classify_call(monkeypatch):
    training.log_session("lift", duration_min=60, rpe=6)
    mock_classify = MagicMock()
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    reply = router.handle_message(CHAT_ID, "no, that was 8 not 6")

    mock_classify.assert_not_called()
    assert "6 -> 8" in reply
    assert _sessions_rows(config.DB_PATH)[0]["rpe"] == 8


def test_correction_amends_todays_pain_only_no_classify_call(monkeypatch):
    training.log_pain("shoulder", 6, side="left")
    mock_classify = MagicMock()
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    reply = router.handle_message(CHAT_ID, "no, that was 8 not 6")

    mock_classify.assert_not_called()
    assert "6 -> 8" in reply
    conn = db.connect(config.DB_PATH)
    try:
        row = conn.execute("SELECT pain_0_10 FROM injury_log ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row["pain_0_10"] == 8


def test_correction_with_both_session_and_pain_asks_which_one(monkeypatch):
    training.log_session("lift", duration_min=60, rpe=6)
    training.log_pain("shoulder", 6, side="left")
    mock_classify = MagicMock()
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    reply = router.handle_message(CHAT_ID, "no, that was 8 not 6")

    mock_classify.assert_not_called()
    assert "Both a session and a pain reading were logged today" in reply
    pending = router._PENDING.get(CHAT_ID)
    assert pending is not None
    assert pending.intent == "CORRECTION"


def test_correction_with_nothing_logged_today_falls_through_to_classify(monkeypatch):
    mock_classify = MagicMock(return_value={
        "intent": "UNCLEAR", "confidence": "low", "extracted": {}, "reasoning": "", "tier": 1,
    })
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    router.handle_message(CHAT_ID, "no, that was 8 not 6")

    mock_classify.assert_called_once()


# ----------------------------------------------------------------------------------
# 5. confirmation policy decision table


@pytest.mark.parametrize("intent", ["CREATE_EVENT", "CREATE_TASK"])
@pytest.mark.parametrize("confidence", ["high", "low"])
def test_needs_confirmation_always_true_for_create_intents(intent, confidence):
    assert router.needs_confirmation(intent, confidence) is True


@pytest.mark.parametrize("confidence", ["high", "low"])
def test_needs_confirmation_unclear_always_true(confidence):
    assert router.needs_confirmation("UNCLEAR", confidence) is True


@pytest.mark.parametrize("intent", ["LOG_PAIN", "LOG_RPE", "LOG_INJURY", "RESOLVE_INJURY"])
def test_needs_confirmation_log_intents_only_on_low_confidence(intent):
    assert router.needs_confirmation(intent, "low") is True
    assert router.needs_confirmation(intent, "high") is False


@pytest.mark.parametrize("intent", ["QUESTION", "CHAT"])
@pytest.mark.parametrize("confidence", ["high", "low"])
def test_needs_confirmation_question_and_chat_never_confirm(intent, confidence):
    assert router.needs_confirmation(intent, confidence) is False


def test_provenance_guard_forces_confirmation_when_value_not_in_message(monkeypatch):
    """The classic confabulation case: the model 'extracted' an rpe of 8 from
    a message that never says 8, so the value must not be trusted or written."""
    mock_classify = MagicMock(return_value={
        "intent": "LOG_RPE", "confidence": "high",
        "extracted": {"rpe": 8, "session_type": "lift", "duration_min": 60},
        "reasoning": "", "tier": 1,
    })
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    reply = router.handle_message(CHAT_ID, "that lift felt pretty rough honestly")

    assert "(yes/no)" in reply
    assert _sessions_rows(config.DB_PATH) == []
    assert router._PENDING.get(CHAT_ID) is not None


# ----------------------------------------------------------------------------------
# 6. core principle: raw text always stored first


def test_classify_raising_falls_back_to_simple_parsing(monkeypatch):
    monkeypatch.setattr(
        router.llm, "classify", MagicMock(side_effect=requests.RequestException("down"))
    )

    reply = router.handle_message(CHAT_ID, "court 90 rpe 6")

    assert "(model unavailable, degraded to simple parsing)" in reply
    rows = _log_entries_rows(config.DB_PATH)
    assert len(rows) == 1
    assert rows[0]["raw_text"] == "court 90 rpe 6"
    assert rows[0]["parse_method"] == "simple"
    assert rows[0]["parse_status"] == "parsed"


def test_handler_exception_still_stores_raw_text_and_marks_error(monkeypatch):
    # Something genuinely inside the pipeline (context assembly, which runs
    # before classification) blows up. classify is never even reached.
    monkeypatch.setattr(
        router.training, "active_injuries", MagicMock(side_effect=RuntimeError("boom"))
    )
    mock_classify = MagicMock()
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    reply = router.handle_message(CHAT_ID, "some message")

    assert "something went wrong handling that" in reply
    assert "nothing was lost" in reply
    mock_classify.assert_not_called()
    rows = _log_entries_rows(config.DB_PATH)
    assert len(rows) == 1
    assert rows[0]["raw_text"] == "some message"
    assert rows[0]["parse_status"] == "error"


def test_insert_failure_reports_not_stored_and_never_dispatches(monkeypatch):
    """The one genuinely bad outcome the module docstring calls out: if even
    storing the raw text fails, say so plainly and do not try to handle it."""
    monkeypatch.setattr(
        router.db, "insert_log_entry", MagicMock(side_effect=RuntimeError("disk full"))
    )
    mock_classify = MagicMock()
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    reply = router.handle_message(CHAT_ID, "anything")

    assert "could not store that message" in reply
    mock_classify.assert_not_called()


# ----------------------------------------------------------------------------------
# 7. regression tests for bugs fixed after adversarial review
#
# Each test below pins down one specific fix. Where the assertion is on the
# private helper directly (detect_correction, _start_task_flow) that mirrors
# how the rest of this file already tests those helpers; where the bug was
# only observable through the full pipeline, the test goes through
# handle_message with llm.classify/llm.chat stubbed, same as section 5 above.


# --- 7.1 detect_correction report-verb exclusion --------------------------------


def test_detect_correction_report_verb_excludes_new_report():
    """'actually my left knee hurts, 3/10' is a fresh reading wearing a
    correction opener, not an amendment: 'hurts' means report, not correct."""
    assert router.detect_correction("actually my left knee hurts, 3/10") is None


def test_detect_correction_body_part_with_no_verb_is_still_a_correction():
    result = router.detect_correction("wrong, that was my left shoulder")
    assert result == {"kind": "body_part", "body_part": "shoulder", "side": "left"}


def test_report_verb_correction_reaches_classify_and_leaves_other_injury_alone(monkeypatch):
    training.log_pain("shoulder", 6, side="right")

    mock_classify = MagicMock(return_value={
        "intent": "LOG_PAIN", "confidence": "high",
        "extracted": {"body_part": "knee", "side": "left", "pain_0_10": 3},
        "reasoning": "", "tier": 1,
    })
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    router.handle_message(CHAT_ID, "actually my left knee hurts, 3/10")

    mock_classify.assert_called_once()
    conn = db.connect(config.DB_PATH)
    try:
        row = conn.execute(
            """
            SELECT l.pain_0_10 FROM injury_log l JOIN injuries i ON i.id = l.injury_id
             WHERE i.body_part = 'shoulder' AND i.side = 'right'
             ORDER BY l.id DESC LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    assert row["pain_0_10"] == 6  # unchanged: the knee reading is a new row, not an edit


# --- 7.2 detect_correction bare-number anchor requirement -----------------------


def test_detect_correction_bare_number_no_anchor_and_too_long_is_none():
    """No 'not <n>' anchor and more than 3 words: read by the classifier
    (which can see calendar context), never silently overwritten."""
    assert router.detect_correction("that was brutal, like an 8") is None


def test_detect_correction_bare_number_with_anchor_is_a_correction():
    assert router.detect_correction("no, that was 8 not 6") == {
        "kind": "rpe_or_pain", "new_value": 8
    }


def test_detect_correction_short_bare_number_is_a_correction():
    assert router.detect_correction("actually 7.5") == {
        "kind": "rpe_or_pain", "new_value": 7.5
    }


def test_unanchored_bare_number_falls_through_and_preserves_todays_rpe(monkeypatch):
    training.log_session("court", duration_min=90, rpe=6)
    mock_classify = MagicMock(return_value={
        "intent": "CHAT", "confidence": "high", "extracted": {}, "reasoning": "", "tier": 1,
    })
    monkeypatch.setattr(router.llm, "classify", mock_classify)
    monkeypatch.setattr(router.llm, "chat", MagicMock(return_value="sounds like a tough one"))

    router.handle_message(CHAT_ID, "that was brutal, like an 8")

    mock_classify.assert_called_once()
    rows = _sessions_rows(config.DB_PATH)
    assert len(rows) == 1
    assert rows[0]["rpe"] == 6  # not silently overwritten to 8


# --- 7.3 rejecting a pending confirm must not fall into the correction path -----


def test_rejecting_pending_confirm_does_not_amend_unrelated_same_day_data(monkeypatch):
    # A different pain entry logged earlier today than the one being confirmed.
    training.log_pain("knee", 3, side="left")

    mock_classify = MagicMock(return_value={
        "intent": "LOG_PAIN", "confidence": "low",
        "extracted": {"body_part": "shoulder", "side": "right", "pain_0_10": 8},
        "reasoning": "", "tier": 1,
    })
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    first_reply = router.handle_message(CHAT_ID, "shoulder 8 maybe")
    assert "(yes/no)" in first_reply
    pending = router._PENDING.get(CHAT_ID)
    assert pending is not None and pending.intent == "LOG_PAIN"

    reply = router.handle_message(CHAT_ID, "no, right shoulder 4")

    mock_classify.assert_called_once()  # only the first message reached classify
    assert "not logged" in reply
    assert router._PENDING.get(CHAT_ID) is None
    conn = db.connect(config.DB_PATH)
    try:
        row = conn.execute(
            """
            SELECT l.pain_0_10 FROM injury_log l JOIN injuries i ON i.id = l.injury_id
             WHERE i.body_part = 'knee' AND i.side = 'left'
             ORDER BY l.id DESC LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    assert row["pain_0_10"] == 3  # untouched by the rejected shoulder confirm


# --- 7.4 decimal correction with both a session and a pain entry today ---------


def test_decimal_correction_with_both_entries_amends_rpe_not_truncated_pain(monkeypatch):
    training.log_session("lift", duration_min=60, rpe=5)
    training.log_pain("ankle", 4)
    # Deterministic correction path should handle this without ever reaching
    # the classifier; if it does, that is the bug (an accidental fall-through).
    mock_classify = MagicMock(side_effect=AssertionError("classify should not be called"))
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    reply = router.handle_message(CHAT_ID, "no, that was 7.5 not 5")

    mock_classify.assert_not_called()
    assert "5 -> 7.5" in reply
    sessions = _sessions_rows(config.DB_PATH)
    assert sessions[0]["rpe"] == 7.5
    conn = db.connect(config.DB_PATH)
    try:
        pain_row = conn.execute(
            "SELECT pain_0_10 FROM injury_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert pain_row["pain_0_10"] == 4  # not truncated to int(7.5) == 7


# --- 7.5 duration provenance/cap in _merge_rpe_fields ---------------------------


def test_merge_rpe_duration_over_cap_and_absent_from_text_is_dropped(monkeypatch):
    mock_classify = MagicMock(return_value={
        "intent": "LOG_RPE", "confidence": "high",
        "extracted": {"rpe": 6, "session_type": "court", "duration_min": 9999},
        "reasoning": "", "tier": 1,
    })
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    router.handle_message(CHAT_ID, "court rpe 6")

    rows = _sessions_rows(config.DB_PATH)
    assert len(rows) == 1
    assert rows[0]["duration_min"] is None
    assert rows[0]["rpe"] == 6


def test_merge_rpe_phantom_in_range_duration_absent_from_text_is_dropped(monkeypatch):
    mock_classify = MagicMock(return_value={
        "intent": "LOG_RPE", "confidence": "high",
        "extracted": {"rpe": 6, "session_type": "court", "duration_min": 45},
        "reasoning": "", "tier": 1,
    })
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    router.handle_message(CHAT_ID, "court rpe 6")

    rows = _sessions_rows(config.DB_PATH)
    assert len(rows) == 1
    assert rows[0]["duration_min"] is None


def test_merge_rpe_duration_present_in_text_survives(monkeypatch):
    mock_classify = MagicMock(return_value={
        "intent": "LOG_RPE", "confidence": "high",
        "extracted": {"rpe": 6, "session_type": "court", "duration_min": 90},
        "reasoning": "", "tier": 1,
    })
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    router.handle_message(CHAT_ID, "court 90 rpe 6")

    rows = _sessions_rows(config.DB_PATH)
    assert len(rows) == 1
    assert rows[0]["duration_min"] == 90


# --- 7.6 LOG_RPE must not complete an RPE-less session of a different type -----


def test_log_rpe_creates_new_session_when_type_differs_from_rpe_less_session(monkeypatch):
    training.log_session("lift", duration_min=60, rpe=None)
    mock_classify = MagicMock(return_value={
        "intent": "LOG_RPE", "confidence": "high",
        "extracted": {"rpe": 7, "session_type": "court"},
        "reasoning": "", "tier": 1,
    })
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    router.handle_message(CHAT_ID, "court felt like a 7")

    rows = _sessions_rows(config.DB_PATH)
    assert len(rows) == 2  # a new court session, not the lift row hijacked
    lift_row = next(r for r in rows if r["type"] == "lift")
    court_row = next(r for r in rows if r["type"] == "court")
    assert lift_row["rpe"] is None
    assert court_row["rpe"] == 7


def test_log_rpe_completes_matching_type_rpe_less_session(monkeypatch):
    training.log_session("lift", duration_min=60, rpe=None)
    mock_classify = MagicMock(return_value={
        "intent": "LOG_RPE", "confidence": "high",
        "extracted": {"rpe": 7, "session_type": "lift"},
        "reasoning": "", "tier": 1,
    })
    monkeypatch.setattr(router.llm, "classify", mock_classify)

    router.handle_message(CHAT_ID, "lift felt like a 7")

    rows = _sessions_rows(config.DB_PATH)
    assert len(rows) == 1  # same session completed, not duplicated
    assert rows[0]["rpe"] == 7


# --- 7.7 CREATE_TASK validation in _start_task_flow -----------------------------


def test_start_task_flow_drops_unknown_type_and_malformed_due_date():
    entry_id = db.insert_log_entry("random task text")

    reply, status = router._start_task_flow(
        CHAT_ID, "random task text", entry_id,
        {
            "title": "read chapter 4",
            "due_date": "not-a-date",
            "course": "ECE-UY 2004",
            "task_type": "homework",  # not in the pset/exam/lab/reading/project/quiz enum
        },
    )

    assert "(yes/no)" in reply
    pending = router._PENDING.get(CHAT_ID)
    assert pending is not None
    assert pending.intent == "CREATE_TASK"
    assert pending.extracted["task_type"] is None
    assert pending.extracted["due_date"] is None


def test_start_task_flow_keeps_valid_type_and_due_date():
    entry_id = db.insert_log_entry("pset 3 due next week")

    router._start_task_flow(
        CHAT_ID, "pset 3 due next week", entry_id,
        {
            "title": "pset 3",
            "due_date": "2026-08-05",
            "course": "ECE-UY 2004",
            "task_type": "pset",
        },
    )

    pending = router._PENDING.get(CHAT_ID)
    assert pending.extracted["task_type"] == "pset"
    assert pending.extracted["due_date"] == "2026-08-05"
