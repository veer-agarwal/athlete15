"""Training and injury logging. Phase 4.

This is the only module that WRITES data you generate yourself, rather than reading
from an external API. That makes it the most valuable table in the database: WHOOP
history can be backfilled with one API call whenever you connect it, but a session
you never logged is gone permanently.

DESIGN CONSTRAINT: logging must take under 15 seconds on a phone. Terse commands,
forgiving parsing, no confirmation prompts for routine entries. If it takes longer
than that you will stop doing it inside two weeks and the whole table is worthless.

FREE TEXT, not slash commands. Anything you send the bot that does not start with a
slash is a log entry. You write "right shoulder 3, court 90 rpe 6" and the parser
takes what it recognizes. This is looser than a command grammar on purpose: a
grammar you have to remember at the end of a lift is a grammar you stop using.

Parsing rules:
    - Every extractor is optional. One unrecognized clause never rejects the rest.
    - The raw text is stored before any of this runs (see db.insert_log_entry), so
      a wrong parse costs accuracy, never the entry.
    - Always report back what was understood AND what was not. A parser that fails
      quietly is worse than one that fails loudly, because you find out months
      later that half your history is missing.

Recognized, roughly:
    right shoulder 3            pain check-in
    shoulder pain 4/10          pain check-in
    tweaked my left ankle       opens an injury
    right shoulder resolved     resolves an injury
    court 90 rpe 6              a session
    lifted 60 min rpe 8         a session
    rpe 7                       a session with no type
"""

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from src import db

# Parser used when a caller does not name one. Swapping this to "llm" once a local
# model is running in phase 7 changes the default everywhere without touching a
# single call site. See parse_entry().
DEFAULT_PARSE_METHOD = "simple"

# Longest first, so "lower back" is matched before "back" and "rotator cuff"
# before nothing. The regex alternation is built in this order and Python's re
# takes the first alternative that matches, not the longest.
BODY_PARTS = (
    "rotator cuff", "lower back", "upper back", "hamstring", "quad", "achilles",
    "shoulder", "forearm", "adductor", "groin", "ankle", "elbow", "wrist", "knee",
    "shin", "calf", "neck", "back", "hip", "heel", "foot", "hand", "thumb",
    "finger", "toe", "lat", "trap", "chest", "bicep", "tricep", "glute",
)

# Words that follow a body part in an exercise name rather than a pain report.
# "shoulder press 3x10" must not become "shoulder pain 3". This is the single
# most likely false positive in the whole parser, because a volleyball lifting
# program is full of exercises named after the joint they load.
_EXERCISE_WORDS = frozenset({
    "press", "raise", "raises", "fly", "flies", "curl", "curls", "extension",
    "extensions", "pull", "pulls", "row", "rows", "shrug", "shrugs", "squat",
    "squats", "thrust", "thrusts", "bridge", "bridges", "carry", "carries",
})

SIDES = {
    "left": "left", "lt": "left", "l": "left",
    "right": "right", "rt": "right", "r": "right",
    "both": "bilateral", "bilateral": "bilateral", "either": "bilateral",
}

# Verbs that mean an injury started. Kept narrow: a false positive here creates a
# row in the injuries table, which is noisier to undo than a missed one.
_OPEN_WORDS = (
    "tweaked", "strained", "sprained", "pulled", "injured", "rolled", "jammed",
    "tore", "hurt", "new injury", "banged up", "dinged",
)

# Verbs that mean an injury ended.
_RESOLVE_WORDS = (
    "resolved", "healed", "cleared", "recovered", "back to normal", "all better",
    "no longer bothering", "good now", "fine now",
)

SESSION_TYPES: dict[str, tuple[str, ...]] = {
    "lift": (
        "lift", "lifted", "lifting", "lifts", "squat", "squatted", "bench",
        "benched", "deadlift", "deadlifted", "weights", "weight room",
    ),
    "court": (
        "court", "open gym", "practice", "practiced", "volleyball", "scrimmage",
        "scrimmaged", "hitting", "serving", "passing", "captains", "match",
        "matches", "game", "games",
    ),
    "conditioning": (
        "cond", "conditioning", "bike", "biked", "run", "ran", "running", "erg",
        "rowed", "intervals", "sprints", "sprinted", "cardio", "swim", "swam",
        "jog", "jogged",
    ),
    "mobility": (
        "mob", "mobility", "stretch", "stretched", "stretching", "yoga", "rehab",
        "prehab", "foam roll", "foam rolled",
    ),
}

# Filler that should not count as "text I did not understand" when deciding
# whether a parse was complete. Without this every entry reports leftovers and
# the reply becomes noise you learn to ignore.
_STOPWORDS = frozenset({
    "a", "about", "after", "all", "also", "am", "an", "and", "around", "at", "back",
    "be", "been", "before", "bit", "but", "by", "day", "did", "do", "doing", "done",
    "evening", "feel", "feeling", "feels", "felt", "fine", "for", "from", "get",
    "good", "got", "had", "has", "have", "he", "her", "him", "his", "i", "in", "is",
    "it", "its", "just", "kind", "like", "little", "lot", "me", "morning", "much",
    "my", "night", "no", "not", "of", "off", "ok", "okay", "on", "only", "or",
    "our", "out", "over", "pm", "pretty", "really", "session", "she", "so", "some",
    "somewhat", "still", "than", "that", "the", "their", "then", "there", "they",
    "this", "through", "to", "today", "tonight", "too", "training", "up", "very",
    "was", "we", "went", "were", "with", "workout", "yesterday", "you", "your",
})

_BODY_PART_ALT = "|".join(re.escape(part) for part in BODY_PARTS)
_SIDE_ALT = "|".join(re.escape(side) for side in sorted(SIDES, key=len, reverse=True))

# An optional side, a body part, then up to a short run of filler, then a 0-10
# score optionally written as "3/10". The filler window is bounded and forbidden
# from containing digits or clause punctuation so a score cannot be dragged in
# from the next clause over.
_PAIN_RE = re.compile(
    rf"\b(?:(?P<side>{_SIDE_ALT})\s+)?"
    rf"(?P<part>{_BODY_PART_ALT})s?\b"
    rf"(?P<gap>[^\d,;.\n]{{0,18}}?)"
    rf"\b(?P<score>10|\d)(?:\s*/\s*10)?\b",
    re.IGNORECASE,
)

# Body part with no number attached, for injury open/resolve clauses.
_PART_RE = re.compile(
    rf"\b(?:(?P<side>{_SIDE_ALT})\s+)?(?P<part>{_BODY_PART_ALT})s?\b",
    re.IGNORECASE,
)

_RPE_RE = re.compile(r"\brpe\s*[:@]?\s*(?P<rpe>10|\d(?:\.\d)?)\b", re.IGNORECASE)
_AT_RPE_RE = re.compile(r"@\s*(?P<rpe>10|\d(?:\.\d)?)\b")

_DURATION_UNIT_RE = re.compile(
    r"\b(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>hours|hour|hrs|hr|h|minutes|minute|mins|min|m)\b",
    re.IGNORECASE,
)
# Bare number directly before an RPE, as in "court 90 rpe 6". CLAUDE.md's target
# command shapes leave the unit off, so bare durations have to work.
_BARE_DURATION_RE = re.compile(r"\b(?P<value>\d{1,3})\s+(?=rpe\b)", re.IGNORECASE)

# Bare number immediately after the activity word, as in "court 90" or "mob 20".
# Adjacency is what makes this safe: in "squat 5x3 225" and "bench 4x5 155" the
# sets and reps sit between the keyword and the number, so the weight is never
# mistaken for a duration. The lookahead rules out a number that already carries
# a non-time unit, so "ran 5 miles" is not a five minute run.
_TRAILING_DURATION_RE = re.compile(
    r"^\s+(?P<value>\d{1,3})\b"
    r"(?!\s*(?:mi|mile|miles|km|m|meters|metres|yards|yds|lb|lbs|kg|k|"
    r"reps?|sets?|x)\b)",
    re.IGNORECASE,
)

# Longest plausible session in minutes. A number above this next to an activity
# word is a weight, a distance, or a typo, and treating it as duration would put
# a garbage figure into the load sums.
MAX_SESSION_MINUTES = 300

# Sets and reps, stripped before pain matching so "5x3" cannot read as a score.
_SETS_REPS_RE = re.compile(r"\b\d+\s*[x×]\s*\d+\b", re.IGNORECASE)


# ----------------------------------------------------------------------------------
# parsing


def parse_entry(raw_text: str, method: str | None = None) -> dict:
    """Turn a free text log entry into structured data.

    The method argument is the extension point. Parsers register in _PARSERS, and
    swapping the default to a local model later means adding one entry there and
    changing DEFAULT_PARSE_METHOD. Callers pass raw text and read the same result
    shape regardless, so nothing downstream changes.

    Args:
        raw_text: the message exactly as sent.
        method: parser name, defaulting to DEFAULT_PARSE_METHOD.

    Returns:
        {
          "parse_method": "simple",
          "parse_status": "parsed" | "partial" | "unparsed",
          "pain":     [{"body_part", "side", "pain_0_10"}],
          "injuries": [{"action": "open"|"resolve", "body_part", "side",
                        "description"}],
          "sessions": [{"type", "duration_min", "rpe"}],
          "unparsed": "text that no extractor claimed",
        }

    Raises:
        ValueError: if method names a parser that is not registered.
    """
    method = method or DEFAULT_PARSE_METHOD
    parser = _PARSERS.get(method)
    if parser is None:
        raise ValueError(
            f"unknown parse method {method!r}, have {sorted(_PARSERS)}"
        )

    result = parser(raw_text)
    result["parse_method"] = method
    return result


def _parse_simple(raw_text: str) -> dict:
    """Regex and keyword parser. No model, no network, deterministic.

    Runs the extractors in a fixed order and records the character span each match
    consumed. Whatever span is left over at the end is what the reply reports as
    not understood, which is more honest than guessing at a confidence score.
    """
    text = raw_text.strip()
    spans: list[tuple[int, int]] = []

    # Sets and reps first, and only to reserve their characters. "bench 4x5 155"
    # must not let the 5 become a pain score or the 155 a duration.
    for match in _SETS_REPS_RE.finditer(text):
        spans.append(match.span())

    injuries = _find_injuries(text, spans)
    pain = _find_pain(text, spans)
    sessions = _find_session(text, spans)

    unparsed = _leftover(text, spans)

    if pain or injuries or sessions:
        status = "partial" if unparsed else "parsed"
    else:
        status = "unparsed"

    return {
        "parse_status": status,
        "pain": pain,
        "injuries": injuries,
        "sessions": sessions,
        "unparsed": unparsed,
    }


# Registry of parsers by method name. An 'llm' entry slots in beside 'simple'
# without any caller needing to know it happened.
_PARSERS: dict[str, Callable[[str], dict]] = {"simple": _parse_simple}


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    """Whether a candidate match collides with something already claimed."""
    return any(span[0] < end and start < span[1] for start, end in spans)


def _find_pain(text: str, spans: list[tuple[int, int]]) -> list[dict]:
    """Pain check-ins: a body part, optional side, and a 0-10 score."""
    reports = []
    for match in _PAIN_RE.finditer(text):
        if _overlaps(match.span(), spans):
            continue

        # "shoulder press 3x10" reaches here with gap=" press " if the sets and
        # reps were not already claimed. Reject on the exercise word regardless.
        gap_words = set(re.findall(r"[a-z]+", match.group("gap").lower()))
        if gap_words & _EXERCISE_WORDS:
            continue
        # "rpe" or a duration unit inside the gap means the number belongs to the
        # session clause, not to the body part.
        if gap_words & {"rpe", "min", "mins", "minutes", "hr", "hrs", "hours"}:
            continue

        side = SIDES.get((match.group("side") or "").lower()) if match.group("side") else None
        reports.append({
            "body_part": match.group("part").lower(),
            "side": side,
            "pain_0_10": int(match.group("score")),
        })
        spans.append(match.span())

    return reports


def _find_injuries(text: str, spans: list[tuple[int, int]]) -> list[dict]:
    """Injury open and resolve clauses.

    Runs before pain matching because "tweaked my right shoulder 4" should open an
    injury AND record the 4, and the injury clause needs first claim on the body
    part so the pain matcher does not consume it alone.
    """
    found = []
    lowered = text.lower()

    for action, words in (("resolve", _RESOLVE_WORDS), ("open", _OPEN_WORDS)):
        for word in words:
            for keyword in re.finditer(rf"\b{re.escape(word)}\b", lowered):
                part_match = _nearest_part(text, keyword.span())
                if part_match is None:
                    continue

                span = (
                    min(keyword.start(), part_match.start()),
                    max(keyword.end(), part_match.end()),
                )
                if _overlaps(span, spans):
                    continue

                side_word = part_match.group("side")
                found.append({
                    "action": action,
                    "body_part": part_match.group("part").lower(),
                    "side": SIDES.get(side_word.lower()) if side_word else None,
                    # The clause itself, not the whole message. Enough to
                    # recognize the injury later without dragging in unrelated
                    # text from the same entry.
                    "description": text[span[0]:span[1]].strip(),
                })
                # Claim the verb but not the body part: a trailing score in
                # "tweaked my ankle 5" should still register as a pain reading.
                spans.append(keyword.span())

    return found


def _nearest_part(text: str, around: tuple[int, int]) -> re.Match | None:
    """The body part mention closest to a keyword, within the same clause.

    Bounded to 40 characters either side and stopped at clause punctuation, so
    "ankle is fine, tweaked something" does not attach the ankle to the tweak.
    """
    start = max(0, around[0] - 40)
    end = min(len(text), around[1] + 40)
    window = text[start:end]

    best = None
    for match in _PART_RE.finditer(window):
        absolute = start + match.start()
        between = text[min(absolute, around[0]):max(absolute, around[1])]
        if any(punct in between for punct in ",;.\n"):
            continue
        if best is None or abs(absolute - around[0]) < abs(best[0] - around[0]):
            best = (start + match.start(), match)

    return best[1] if best else None


def _find_session(text: str, spans: list[tuple[int, int]]) -> list[dict]:
    """A single training session: type, duration, RPE.

    One session per entry. Two sessions in one message would need clause splitting
    that the reply-and-correct loop makes unnecessary: log them as two messages.
    """
    # Activity word first, because the bare duration rule below is defined
    # relative to where it sits.
    session_type = None
    type_span = None
    for name, keywords in SESSION_TYPES.items():
        for keyword in keywords:
            match = re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE)
            if match and not _overlaps(match.span(), spans):
                session_type = name
                type_span = match.span()
                spans.append(match.span())
                break
        if session_type:
            break

    rpe = None
    for pattern in (_RPE_RE, _AT_RPE_RE):
        match = pattern.search(text)
        if match and not _overlaps(match.span(), spans):
            value = float(match.group("rpe"))
            # An out of range RPE is a typo, not data. Leave it unclaimed so it
            # surfaces in the unparsed text instead of poisoning the load sums.
            if 0 < value <= 10:
                rpe = int(value) if value == int(value) else value
                spans.append(match.span())
            break

    duration = None
    for match in _DURATION_UNIT_RE.finditer(text):
        if _overlaps(match.span(), spans):
            continue
        value = float(match.group("value"))
        unit = match.group("unit").lower()
        duration = round(value * 60) if unit.startswith("h") else round(value)
        spans.append(match.span())
        break

    if duration is None:
        duration = _bare_duration(text, type_span, spans)

    if session_type is None and duration is None and rpe is None:
        return []

    return [{
        # "other" rather than dropping it: an RPE with no recognizable activity
        # is still load, and load with an unknown type still belongs in the sums.
        "type": session_type or "other",
        "duration_min": duration,
        "rpe": rpe,
    }]


def _bare_duration(
    text: str, type_span: tuple[int, int] | None, spans: list[tuple[int, int]]
) -> int | None:
    """A duration written with no unit, in the two positions where it is safe.

    Directly before an RPE ("court 90 rpe 6") or directly after the activity word
    ("mob 20"). Anywhere else a loose number is far more likely to be a weight or
    a rep count, and guessing wrong there feeds a wrong figure into the load sums
    where nothing would ever flag it.
    """
    match = _BARE_DURATION_RE.search(text)
    if match and not _overlaps(match.span(), spans):
        value = int(match.group("value"))
        if 0 < value <= MAX_SESSION_MINUTES:
            spans.append(match.span())
            return value

    if type_span is not None:
        trailing = _TRAILING_DURATION_RE.match(text[type_span[1]:])
        if trailing:
            span = (type_span[1] + trailing.start("value"),
                    type_span[1] + trailing.end("value"))
            value = int(trailing.group("value"))
            if not _overlaps(span, spans) and 0 < value <= MAX_SESSION_MINUTES:
                spans.append(span)
                return value

    return None


def _leftover(text: str, spans: list[tuple[int, int]]) -> str:
    """Text no extractor claimed, with filler words removed.

    Returned as the original fragments rather than a token list, because reading
    "not understood: felt heavy off the block" tells you what to rephrase and
    "not understood: heavy block" does not.
    """
    chars = list(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(chars), end)):
            chars[index] = " "

    fragments = []
    for fragment in re.split(r"[,;.\n]+", "".join(chars)):
        tokens = [
            token for token in re.findall(r"[a-z0-9']+", fragment.lower())
            if len(token) > 1 and token not in _STOPWORDS
        ]
        if tokens:
            fragments.append(" ".join(fragment.split()))

    return "; ".join(fragments)


# ----------------------------------------------------------------------------------
# applying a parse


def apply_entry(result: dict, on_date: str | None = None) -> list[str]:
    """Write a parse result to the database. Returns one line per thing written.

    Injuries are applied before pain, so "tweaked my right shoulder 4" opens the
    injury first and the pain reading then attaches to it rather than creating a
    second one.
    """
    on_date = on_date or _today()
    applied = []

    for injury in result.get("injuries", []):
        if injury["action"] == "open":
            open_injury(
                injury["body_part"],
                injury.get("side"),
                injury.get("description", ""),
                on_date=on_date,
            )
            applied.append(f"injury opened: {_describe_part(injury)}")
        else:
            resolved = resolve_injury_by_part(
                injury["body_part"], injury.get("side"), on_date=on_date
            )
            if resolved:
                applied.append(f"injury resolved: {_describe_part(injury)}")
            else:
                applied.append(
                    f"no active injury matching {_describe_part(injury)} to resolve"
                )

    for report in result.get("pain", []):
        log_pain(
            report["body_part"],
            report["pain_0_10"],
            report.get("side"),
            on_date=on_date,
        )
        applied.append(
            f"pain: {_describe_part(report)} {report['pain_0_10']}/10"
        )

    for session in result.get("sessions", []):
        log_session(
            session["type"],
            duration_min=session.get("duration_min"),
            rpe=session.get("rpe"),
            on_date=on_date,
        )
        applied.append(f"session: {_describe_session(session)}")

    return applied


def _describe_part(item: dict) -> str:
    side = item.get("side")
    return f"{side} {item['body_part']}" if side else item["body_part"]


def _describe_session(session: dict) -> str:
    parts = [session["type"]]
    if session.get("duration_min") is not None:
        parts.append(f"{session['duration_min']} min")
    if session.get("rpe") is not None:
        parts.append(f"RPE {session['rpe']}")
    else:
        parts.append("no RPE")
    return " ".join(parts)


def format_parse_reply(result: dict, applied: list[str], entry_id: int) -> str:
    """The confirmation sent back to Telegram.

    Always states the entry id and always states that the raw text was kept. The
    point is that a bad parse is visibly a bad parse at the moment you send it,
    while you still remember what you meant, rather than a silent gap you find in
    the data in March.
    """
    lines = []

    if applied:
        lines.append("logged:")
        lines.extend(f"- {item}" for item in applied)
    else:
        lines.append("nothing recognized in that entry")

    if result.get("unparsed"):
        lines.append(f"not understood: {result['unparsed']}")

    lines.append(f"raw text kept as entry #{entry_id}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------
# writing


def _today() -> str:
    """Local date. Sessions are logged against the day you trained, not UTC."""
    from zoneinfo import ZoneInfo

    from src import config

    return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    conn = db.connect()
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO sessions
                    (date, type, block, location, duration_min, rpe, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    on_date or _today(),
                    session_type,
                    block,
                    location,
                    duration_min,
                    rpe,
                    notes,
                    _now(),
                ),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def log_lifts(session_id: int, lifts: list[dict]) -> None:
    """Attach individual exercises to a session.

    Each dict: {"exercise": str, "sets": int, "reps": int, "weight_lb": float}
    """
    conn = db.connect()
    try:
        with conn:
            conn.executemany(
                """
                INSERT INTO lifts (session_id, exercise, sets, reps, weight_lb, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        session_id,
                        lift["exercise"],
                        lift.get("sets"),
                        lift.get("reps"),
                        lift.get("weight_lb"),
                        lift.get("notes"),
                    )
                    for lift in lifts
                ],
            )
    finally:
        conn.close()


def log_pain(
    body_part: str,
    pain_0_10: int,
    side: str | None = None,
    on_date: str | None = None,
    limited: bool | None = None,
) -> None:
    """Record a pain check-in against the matching active injury.

    Creates the injury implicitly when nothing matches. The stub asked which way
    to go here and speed wins: being asked "which injury?" at the end of a lift is
    exactly the friction that kills the habit. A spurious injury row is cheap to
    resolve later, a check-in you abandoned mid-typing is gone.
    """
    injury_id = _match_active_injury(body_part, side)
    if injury_id is None:
        injury_id = open_injury(
            body_part, side, description="opened by a pain check-in", on_date=on_date
        )
        logging.info("opened injury %d implicitly for %s", injury_id, body_part)

    conn = db.connect()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO injury_log (injury_id, date, pain_0_10, limited, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    injury_id,
                    on_date or _today(),
                    pain_0_10,
                    None if limited is None else int(limited),
                    None,
                ),
            )
    finally:
        conn.close()


def _match_active_injury(body_part: str, side: str | None) -> int | None:
    """The id of an open injury for this body part, or None.

    Side is matched loosely. "shoulder 3" after opening a right shoulder injury
    should land on that injury rather than opening a second, sideless one, so a
    missing side on either row is treated as a match.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT id, side FROM injuries
             WHERE body_part = ? AND status IN ('active', 'managing')
             ORDER BY onset_date DESC
            """,
            (body_part,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        if side is None or row["side"] is None or row["side"] == side:
            return int(row["id"])
    return None


def open_injury(
    body_part: str,
    side: str | None = None,
    description: str = "",
    on_date: str | None = None,
) -> int:
    """Create a new active injury. Returns its id."""
    conn = db.connect()
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO injuries
                    (body_part, side, description, onset_date, status, created_at)
                VALUES (?, ?, ?, ?, 'active', ?)
                """,
                (body_part, side, description, on_date or _today(), _now()),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def resolve_injury(injury_id: int, on_date: str | None = None) -> None:
    """Mark an injury resolved and stamp the resolution date."""
    conn = db.connect()
    try:
        with conn:
            conn.execute(
                """
                UPDATE injuries
                   SET status = 'resolved', resolved_date = ?
                 WHERE id = ?
                """,
                (on_date or _today(), injury_id),
            )
    finally:
        conn.close()


def resolve_injury_by_part(
    body_part: str, side: str | None = None, on_date: str | None = None
) -> bool:
    """Resolve the matching open injury. False if there was nothing to resolve."""
    injury_id = _match_active_injury(body_part, side)
    if injury_id is None:
        return False
    resolve_injury(injury_id, on_date=on_date)
    return True


# ----------------------------------------------------------------------------------
# reading and analysis


def sessions_between(start: str, end: str) -> list[dict]:
    """Sessions with date between start and end inclusive. ISO date strings."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE date BETWEEN ? AND ? ORDER BY date",
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def session_load(session: dict) -> float:
    """Session RPE load = duration_min * rpe.

    Returns 0.0 if either value is missing, so unlogged RPE does not corrupt the
    rolling sums below.
    """
    duration = session.get("duration_min")
    rpe = session.get("rpe")
    if duration is None or rpe is None:
        return 0.0
    return float(duration) * float(rpe)


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
    today = date.fromisoformat(on_date) if on_date else date.fromisoformat(_today())

    acute_sessions = sessions_between(
        (today - timedelta(days=6)).isoformat(), today.isoformat()
    )
    chronic_sessions = sessions_between(
        (today - timedelta(days=27)).isoformat(), today.isoformat()
    )

    acute = sum(session_load(s) for s in acute_sessions)
    # 28 day total scaled to a 7 day window, so the two numbers are comparable.
    chronic = sum(session_load(s) for s in chronic_sessions) / 4.0

    return {
        "acute": round(acute, 1),
        "chronic": round(chronic, 1),
        "ratio": round(acute / chronic, 2) if chronic else None,
    }


def active_injuries() -> list[dict]:
    """All injuries with status active or managing, with latest pain reading."""
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT i.*,
                   (SELECT pain_0_10 FROM injury_log l
                     WHERE l.injury_id = i.id
                     ORDER BY l.date DESC, l.id DESC LIMIT 1) AS latest_pain,
                   (SELECT date FROM injury_log l
                     WHERE l.injury_id = i.id
                     ORDER BY l.date DESC, l.id DESC LIMIT 1) AS latest_pain_date
              FROM injuries i
             WHERE i.status IN ('active', 'managing')
             ORDER BY i.onset_date
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def format_training_block() -> str:
    """The training section of the morning briefing.

    Numbers only, no verdict, per CLAUDE.md. The ratio is stated next to the two
    loads it came from precisely so it reads as a description of what you did
    rather than as a threshold you crossed.
    """
    lines = []
    today = date.fromisoformat(_today())

    yesterday = (today - timedelta(days=1)).isoformat()
    logged = sessions_between(yesterday, yesterday)
    if logged:
        # Joined onto one line rather than repeating the label, since two-a-days
        # are normal in season and three "Yesterday:" lines read as a bug.
        lines.append(
            "Yesterday: " + "; ".join(_describe_session(s) for s in logged)
        )
    else:
        lines.append("Yesterday: nothing logged")

    load = acute_chronic_ratio()
    ratio = f"{load['ratio']:.2f}" if load["ratio"] is not None else "n/a"
    lines.append(
        f"7d load {load['acute']:.0f}, 28d avg scaled to 7d {load['chronic']:.0f}, "
        f"ratio {ratio}"
    )

    injuries = active_injuries()
    if injuries:
        for injury in injuries:
            days = _days_since(injury["onset_date"], today)
            pain = (
                f"pain {injury['latest_pain']}/10"
                if injury["latest_pain"] is not None
                else "no pain logged"
            )
            lines.append(
                f"Active: {_describe_part(injury)}, {pain}, day {days}"
            )
    else:
        lines.append("Active: none")

    return "\n".join(lines)


def _days_since(onset: str, today: date) -> int:
    """Days an injury has been open, counting the onset day as day 1."""
    return (today - date.fromisoformat(onset)).days + 1
