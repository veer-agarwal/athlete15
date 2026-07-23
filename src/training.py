"""Training and injury logging. Phase 4.

This is the only module that WRITES data you generate yourself, rather than reading
from an external API. That makes it the most valuable table in the database: WHOOP
history can be backfilled with one API call whenever you connect it, but a session
you never logged is gone permanently.

DESIGN CONSTRAINT: logging must take under 15 seconds on a phone. Terse commands,
forgiving parsing, no confirmation prompts for routine entries. If it takes longer
than that you will stop doing it inside two weeks and the whole table is worthless.

Command shapes to support:

    /lift squat 5x3 225, bench 4x5 155, rpe 7
    /court 90 rpe 6 open gym
    /cond 30 rpe 8 bike intervals
    /mob 20
    /pain shoulder 3
    /injury new right shoulder tendinitis
    /injury resolve right shoulder

Parsing rules:
    - RPE optional. Default to NULL, do not reject the entry.
    - Accept "5x3" and "5 x 3" and "5X3".
    - Assume pounds. Accept a trailing "kg" and convert.
    - On a parse failure, reply with what WAS understood and what was not.
      Never silently discard a logged session.
"""

from datetime import date, timedelta

from src import db


# --- writing ---

def log_session(
    session_type: str,
    duration_min: int | None = None,
    rpe: int | None = None,
    block: str | None = None,
    location: str | None = None,
    notes: str | None = None,
    on_date: str | None = None,
) -> int:
    """Insert a training session. Returns the new session id."""
    raise NotImplementedError("phase 4")


def log_lifts(session_id: int, lifts: list[dict]) -> None:
    """Attach individual exercises to a session.

    Each dict: {"exercise": str, "sets": int, "reps": int, "weight_lb": float}
    """
    raise NotImplementedError("phase 4")


def log_pain(body_part: str, pain_0_10: int, side: str | None = None) -> None:
    """Record a daily pain check-in against the matching active injury.

    If no active injury matches the body part, decide what to do. Options are to
    create one implicitly or to reply asking. Creating implicitly is faster, which
    matters more than tidiness here.
    """
    raise NotImplementedError("phase 4")


def open_injury(body_part: str, side: str, description: str = "") -> int:
    """Create a new active injury. Returns its id."""
    raise NotImplementedError("phase 4")


def resolve_injury(injury_id: int) -> None:
    """Mark an injury resolved and stamp the resolution date."""
    raise NotImplementedError("phase 4")


# --- parsing ---

def parse_lift_command(text: str) -> dict:
    """Parse '/lift squat 5x3 225, bench 4x5 155, rpe 7' into structured data.

    Returns:
        {"lifts": [{"exercise": "squat", "sets": 5, "reps": 3, "weight_lb": 225.0},
                   ...],
         "rpe": 7,
         "unparsed": ["..."]}

    Put anything you could not interpret in "unparsed" so the caller can report it
    back instead of dropping it.
    """
    raise NotImplementedError("phase 4")


# --- reading and analysis ---

def sessions_between(start: str, end: str) -> list[dict]:
    """Sessions with date between start and end inclusive. ISO date strings."""
    raise NotImplementedError("phase 4")


def session_load(session: dict) -> float:
    """Session RPE load = duration_min * rpe.

    Returns 0.0 if either value is missing, so unlogged RPE does not corrupt the
    rolling sums below.
    """
    raise NotImplementedError("phase 4")


def acute_chronic_ratio(on_date: str | None = None) -> dict:
    """Acute (7 day) vs chronic (28 day, scaled to 7) training load.

    Returns:
        {"acute": float, "chronic": float, "ratio": float | None}

    Ratio is None when chronic load is zero, which is the case for your first four
    weeks of logging. Handle that rather than dividing by zero.

    IMPORTANT: report this as a descriptive trend only. The research on
    acute:chronic workload ratio as an injury predictor is contested and it is not
    a reliable warning system. State the numbers. Do not render a verdict.
    """
    raise NotImplementedError("phase 4")


def active_injuries() -> list[dict]:
    """All injuries with status active or managing, with latest pain reading."""
    raise NotImplementedError("phase 4")


def format_training_block() -> str:
    """The training section of the morning briefing.

    Roughly:
        Yesterday: lift 65min RPE 7 (facility)
        7d load 1840, 28d avg 1610, ratio 1.14
        Active: right shoulder, pain 3/10, day 12
    """
    raise NotImplementedError("phase 4")
