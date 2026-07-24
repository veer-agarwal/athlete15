"""Unit tests for the free text log parser.

Real unit tests: no network, no database, no Telegram. parse_entry() is a pure
function by design so this file can stay that way.

Run with:  pytest tests/test_parse_entry.py
"""

import pytest

from src import training


def only(items, kind):
    """The single expected item of a kind, with a readable failure if not."""
    assert len(items) == 1, f"expected exactly one {kind}, got {items}"
    return items[0]


# --- pain ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, part, side, score",
    [
        ("right shoulder 3", "shoulder", "right", 3),
        ("shoulder 3", "shoulder", None, 3),
        ("left knee 7", "knee", "left", 7),
        ("shoulder pain 4/10", "shoulder", None, 4),
        ("right shoulder pain 4/10", "shoulder", "right", 4),
        ("my lower back is at a 6", "lower back", None, 6),
        ("r ankle 2", "ankle", "right", 2),
        ("both shoulders 5", "shoulder", "bilateral", 5),
        ("achilles 0", "achilles", None, 0),
        ("knee 10", "knee", None, 10),
    ],
)
def test_pain_shapes(text, part, side, score):
    result = training.parse_entry(text)
    report = only(result["pain"], "pain report")
    assert report["body_part"] == part
    assert report["side"] == side
    assert report["pain_0_10"] == score


def test_longest_body_part_wins():
    """'lower back' must not be parsed as 'back'."""
    report = only(training.parse_entry("lower back 4")["pain"], "pain report")
    assert report["body_part"] == "lower back"


def test_two_pain_reports_in_one_entry():
    result = training.parse_entry("right shoulder 3, left knee 5")
    assert len(result["pain"]) == 2
    assert {r["body_part"] for r in result["pain"]} == {"shoulder", "knee"}


def test_exercise_name_is_not_a_pain_report():
    """'shoulder press 3x10 95' is a lift, not a shoulder at 3/10."""
    result = training.parse_entry("shoulder press 3x10 95")
    assert result["pain"] == []


def test_sets_and_reps_do_not_become_a_pain_score():
    result = training.parse_entry("bench 4x5 155")
    assert result["pain"] == []


def test_body_part_without_a_number_is_not_a_pain_report():
    result = training.parse_entry("shoulder feels good")
    assert result["pain"] == []


def test_score_from_the_next_clause_is_not_pulled_in():
    """The comma stops 'shoulder' from claiming the 6 that belongs to the RPE."""
    result = training.parse_entry("shoulder felt fine, court 90 rpe 6")
    assert result["pain"] == []
    assert only(result["sessions"], "session")["rpe"] == 6


def test_rpe_number_does_not_become_pain():
    result = training.parse_entry("back squat day rpe 8")
    assert result["pain"] == []


# --- sessions --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, kind, duration, rpe",
    [
        ("court 90 rpe 6", "court", 90, 6),
        ("lifted 60 min rpe 8", "lift", 60, 8),
        ("cond 30 rpe 8 bike intervals", "conditioning", 30, 8),
        ("mob 20", "mobility", 20, None),
        ("open gym 2h rpe 5", "court", 120, 5),
        ("ran 45 minutes", "conditioning", 45, None),
        ("practice rpe 7", "court", None, 7),
        ("stretched 15 min", "mobility", 15, None),
        ("1.5h court rpe 6", "court", 90, 6),
    ],
)
def test_session_shapes(text, kind, duration, rpe):
    session = only(training.parse_entry(text)["sessions"], "session")
    assert session["type"] == kind
    assert session["duration_min"] == duration
    assert session["rpe"] == rpe


def test_rpe_alone_still_records_load():
    """An RPE with no recognizable activity is still load worth keeping."""
    session = only(training.parse_entry("rpe 7")["sessions"], "session")
    assert session["type"] == "other"
    assert session["rpe"] == 7


def test_at_sign_rpe():
    session = only(training.parse_entry("court 90 @7")["sessions"], "session")
    assert session["rpe"] == 7


def test_half_point_rpe():
    session = only(training.parse_entry("lifted 60 min rpe 7.5")["sessions"], "session")
    assert session["rpe"] == 7.5


def test_out_of_range_rpe_is_rejected_not_stored():
    """A typo must not silently enter the load sums."""
    result = training.parse_entry("court 90 rpe 70")
    session = only(result["sessions"], "session")
    assert session["rpe"] is None
    assert "70" in result["unparsed"]


def test_missing_rpe_is_allowed():
    session = only(training.parse_entry("court 90")["sessions"], "session")
    assert session["rpe"] is None
    assert session["duration_min"] == 90


def test_no_session_when_nothing_session_like():
    assert training.parse_entry("right shoulder 3")["sessions"] == []


@pytest.mark.parametrize(
    "text",
    ["squat 5x3 225", "bench 4x5 155", "deadlift 315", "ran 5 miles", "biked 10 km"],
)
def test_weights_and_distances_are_not_durations(text):
    """A wrong duration feeds the load sums, where nothing would ever flag it."""
    session = only(training.parse_entry(text)["sessions"], "session")
    assert session["duration_min"] is None


def test_absurd_duration_is_rejected():
    session = only(training.parse_entry("court 900")["sessions"], "session")
    assert session["duration_min"] is None


# --- injuries --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, action, part, side",
    [
        ("tweaked my right shoulder", "open", "shoulder", "right"),
        ("rolled my left ankle", "open", "ankle", "left"),
        ("strained my hamstring", "open", "hamstring", None),
        ("right shoulder resolved", "resolve", "shoulder", "right"),
        ("left ankle healed", "resolve", "ankle", "left"),
        ("knee is cleared", "resolve", "knee", None),
    ],
)
def test_injury_shapes(text, action, part, side):
    injury = only(training.parse_entry(text)["injuries"], "injury")
    assert injury["action"] == action
    assert injury["body_part"] == part
    assert injury["side"] == side


def test_injury_and_pain_in_one_entry():
    """Opening an injury and recording its first pain reading together."""
    result = training.parse_entry("tweaked my right shoulder 4")
    injury = only(result["injuries"], "injury")
    report = only(result["pain"], "pain report")
    assert injury["action"] == "open"
    assert injury["body_part"] == "shoulder"
    assert report["pain_0_10"] == 4


def test_injury_verb_does_not_reach_across_a_clause():
    result = training.parse_entry("ankle is fine, tweaked something in the gym")
    assert result["injuries"] == []


def test_injury_keyword_with_no_body_part_is_not_an_injury():
    result = training.parse_entry("tweaked something")
    assert result["injuries"] == []


# --- status and leftovers --------------------------------------------------------


def test_status_parsed_when_everything_was_claimed():
    result = training.parse_entry("court 90 rpe 6")
    assert result["parse_status"] == "parsed"
    assert result["unparsed"] == ""


def test_status_partial_reports_the_leftover_text():
    result = training.parse_entry("court 90 rpe 6, bus was late again")
    assert result["parse_status"] == "partial"
    assert "bus" in result["unparsed"]


def test_status_unparsed_when_nothing_matched():
    result = training.parse_entry("felt weird about the whole thing")
    assert result["parse_status"] == "unparsed"
    assert result["pain"] == []
    assert result["sessions"] == []
    assert result["injuries"] == []


def test_filler_words_do_not_count_as_unparsed():
    """Otherwise every entry reports leftovers and the reply becomes noise."""
    result = training.parse_entry("did court 90 rpe 6 today")
    assert result["parse_status"] == "parsed"


def test_empty_entry_is_unparsed_not_an_error():
    result = training.parse_entry("")
    assert result["parse_status"] == "unparsed"


# --- method dispatch -------------------------------------------------------------


def test_parse_method_is_reported():
    assert training.parse_entry("court 90 rpe 6")["parse_method"] == "simple"


def test_method_can_be_named_explicitly():
    assert training.parse_entry("rpe 7", method="simple")["parse_method"] == "simple"


def test_unknown_method_raises():
    with pytest.raises(ValueError, match="unknown parse method"):
        training.parse_entry("rpe 7", method="llm")


def test_a_new_method_needs_no_caller_change():
    """The extension point: register a parser, callers are untouched.

    This is what 'swap in an llm method later' has to mean in practice, so it is
    worth a test rather than a comment.
    """
    training._PARSERS["fake"] = lambda text: {
        "parse_status": "parsed",
        "pain": [],
        "injuries": [],
        "sessions": [],
        "unparsed": "",
    }
    try:
        result = training.parse_entry("anything at all", method="fake")
        assert result["parse_method"] == "fake"
        assert result["parse_status"] == "parsed"
    finally:
        del training._PARSERS["fake"]


# --- result shape ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["", "court 90 rpe 6", "right shoulder 3", "gibberish here", "tweaked my ankle"],
)
def test_result_always_has_every_key(text):
    """Callers index these unconditionally, so none may ever be absent."""
    result = training.parse_entry(text)
    for key in (
        "parse_method", "parse_status", "pain", "injuries", "sessions", "unparsed"
    ):
        assert key in result
    assert result["parse_status"] in ("parsed", "partial", "unparsed")


def test_result_is_json_serializable():
    """It goes straight into log_entries.parsed_json."""
    import json

    json.dumps(training.parse_entry("court 90 rpe 6, right shoulder 3"))
