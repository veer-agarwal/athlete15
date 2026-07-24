"""Intent router. Phase 7.

Every inbound Telegram message lands here (notify wraps handle_message in
asyncio.to_thread, so everything in this module is plain synchronous code).

CORE PRINCIPLE, non-negotiable: the raw text is written to log_entries BEFORE
any classification, pending-state handling, or parsing. Classification is a
separate step that is allowed to fail; a message is never lost regardless of
what the model does. This is the same raw-first convention training.py and
notify.py already follow, applied to the router.

Pipeline for one message:

    1. store raw text                      (unconditional, first)
    2. pending question open?              -> interpret as its answer, or drop
    3. deterministic correction?           -> amend today's data, no model
    4. gather context, llm.classify        -> intent + extracted fields
    5. confirmation gate                   -> pending question, or
    6. handler                             -> write / create / chat / question
    7. record_parse on the stored entry

The model being down at step 4 degrades to training.parse_entry, the phase 4
regex parser, with an honest note in the reply. Never a lost message.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

from src import config, db, llm, training
from src.sources import calendar as calendar_source
from src.sources import notion as notion_source

# How long a pending question stays answerable. Five minutes: long enough to
# reply from a phone, short enough that tomorrow's "yes" to something else
# cannot land on a stale question.
PENDING_TTL_SECONDS = 300

# Calendar fetch cache lifetime. Every inbound text triggers context assembly,
# and a CalDAV round trip is about 2 seconds; uncached it would eat most of
# the 15 second logging budget on every single message.
CALENDAR_CACHE_TTL_SECONDS = 600

# An event that ended within this window still counts as context ("just
# finished Lift") when attributing an RPE with no stated session type.
RECENT_EVENT_WINDOW_HOURS = 3

_LOG_INTENTS = frozenset({"LOG_PAIN", "LOG_RPE", "LOG_INJURY", "RESOLVE_INJURY"})
_ALWAYS_CONFIRM = frozenset({"CREATE_EVENT", "CREATE_TASK", "UNCLEAR"})

_YES_WORDS = frozenset({"yes", "y", "yeah", "yep", "ok", "1"})
_NO_WORDS = frozenset({"no", "n", "nah", "nope", "2"})

# Options offered when the classifier could not tell what a message was.
_UNCLEAR_OPTIONS = (
    "log it as training or pain",
    "add a calendar event",
    "create a task",
    "something else",
)


# ----------------------------------------------------------------------------------
# pending state


@dataclass
class Pending:
    """One open question waiting for the next message to answer it."""

    question: str
    kind: str  # "confirm" | "choice"
    options: list[str]
    raw_text: str  # the message that provoked the question
    entry_id: int  # log_entries id of that message
    intent: str
    extracted: dict
    created: float  # monotonic timestamp from the store's clock


class PendingStore:
    """Per-chat pending questions with TTL expiry.

    The clock is injectable (default time.monotonic) so tests can control
    expiry without sleeping. One item per chat id; put replaces, never stacks.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[int, Pending] = {}

    def put(self, chat_id: int, pending: Pending) -> None:
        """Store a pending question, replacing any existing one for the chat."""
        self._items[chat_id] = pending

    def open(
        self,
        chat_id: int,
        question: str,
        kind: str,
        options: list[str],
        raw_text: str,
        entry_id: int,
        intent: str,
        extracted: dict,
    ) -> Pending:
        """Create a Pending stamped with this store's clock and put() it."""
        pending = Pending(
            question=question,
            kind=kind,
            options=options,
            raw_text=raw_text,
            entry_id=entry_id,
            intent=intent,
            extracted=extracted,
            created=self._clock(),
        )
        self.put(chat_id, pending)
        return pending

    def get(self, chat_id: int) -> Pending | None:
        """The chat's pending question, or None. Expired items are cleared."""
        item = self._items.get(chat_id)
        if item is None:
            return None
        if self._clock() - item.created > PENDING_TTL_SECONDS:
            del self._items[chat_id]
            return None
        return item

    def clear(self, chat_id: int) -> None:
        self._items.pop(chat_id, None)


_PENDING = PendingStore()


# ----------------------------------------------------------------------------------
# calendar cache and context gathering


class _CalendarCache:
    """Today's events, refetched at most every CALENDAR_CACHE_TTL_SECONDS.

    A failed fetch is cached too: if iCloud is down, retrying it on every
    message would add a 15 second timeout per text, which is worse than stale
    "calendar unavailable" context for ten minutes.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._events: list[dict] = []
        self._unavailable: bool = False
        self._fetched_at: float | None = None

    def get(self) -> tuple[list[dict], bool]:
        """(events, unavailable). events is [] when unavailable is True."""
        now = self._clock()
        if (
            self._fetched_at is not None
            and now - self._fetched_at < CALENDAR_CACHE_TTL_SECONDS
        ):
            return self._events, self._unavailable

        try:
            self._events = calendar_source.fetch()
            self._unavailable = False
        except Exception as exc:
            logging.warning("router: calendar fetch failed: %s", exc)
            self._events = []
            self._unavailable = True
        self._fetched_at = now
        return self._events, self._unavailable


_CAL_CACHE = _CalendarCache()


def _parse_utc(value: str) -> datetime:
    """UTC ISO-8601 string (possibly Z-suffixed) to an aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fmt_time(dt: datetime) -> str:
    """3:05 PM style. strftime %I zero-pads and %-I is not portable to Windows."""
    return dt.strftime("%I:%M %p").lstrip("0")


def _local_today() -> str:
    return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")


def _rpe_logged_today(local_date: str) -> bool:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE date = ? AND rpe IS NOT NULL LIMIT 1",
            (local_date,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def gather_context(now: datetime | None = None) -> dict:
    """Everything the classifier gets told about the current moment.

    Pure Python and local reads plus the cached calendar fetch; runs before
    the model so the model never has to guess what time it is.
    """
    tz = ZoneInfo(config.TIMEZONE)
    now = now.astimezone(tz) if now is not None else datetime.now(tz)

    events, unavailable = _CAL_CACHE.get()

    current_event: dict | None = None
    recent_events: list[dict] = []
    next_event: dict | None = None
    next_start: datetime | None = None
    window_start = now - timedelta(hours=RECENT_EVENT_WINDOW_HOURS)

    for row in events:
        # All-day events never count as current: "Mom's birthday" containing
        # 3 PM must not become "you are currently in Mom's birthday".
        if row.get("all_day"):
            continue
        try:
            start = _parse_utc(row["start_utc"]).astimezone(tz)
            end = _parse_utc(row["end_utc"]).astimezone(tz) if row.get("end_utc") else start
        except (ValueError, KeyError) as exc:
            logging.warning("router: skipping unparseable event in context: %s", exc)
            continue

        summary = {
            "summary": row.get("summary") or "",
            "location": row.get("location"),
            "start_local": start.isoformat(),
            "end_local": end.isoformat(),
        }
        if start <= now < end:
            current_event = summary
        elif window_start <= end <= now:
            recent_events.append(summary)
        elif start > now and (next_start is None or start < next_start):
            next_event = summary
            next_start = start

    injuries = [
        {
            "body_part": injury["body_part"],
            "side": injury["side"],
            "latest_pain": injury["latest_pain"],
            "latest_pain_date": injury["latest_pain_date"],
        }
        for injury in training.active_injuries()
    ]

    return {
        "now_local": now.isoformat(),
        "weekday": now.strftime("%A"),
        "timezone": config.TIMEZONE,
        "current_event": current_event,
        "recent_events": recent_events,
        "next_event": next_event,
        "rpe_logged_today": _rpe_logged_today(now.strftime("%Y-%m-%d")),
        "active_injuries": injuries,
        "calendar_unavailable": unavailable,
    }


def _describe_event(event: dict) -> str:
    """'Lift 3:00 PM-4:00 PM at Palladium' from a context event dict."""
    start = datetime.fromisoformat(event["start_local"])
    end = datetime.fromisoformat(event["end_local"])
    text = f"{event['summary']} {_fmt_time(start)}-{_fmt_time(end)}"
    if event.get("location"):
        text += f" at {event['location']}"
    return text


def _describe_side_part(side: str | None, body_part: str | None) -> str:
    part = body_part or "?"
    return f"{side} {part}" if side else part


def format_context(context: dict) -> str:
    """Render the context as a compact labeled block.

    Labeled uppercase lines rather than prose because small models follow
    labeled blocks far more reliably; same reasoning as the briefing template.
    """
    lines = [
        f"NOW: {context['now_local']} ({context['weekday']})",
        f"TIMEZONE: {context['timezone']}",
    ]

    if context.get("calendar_unavailable"):
        lines.append("CALENDAR: unavailable")
    else:
        current = context.get("current_event")
        lines.append(
            f"CURRENT EVENT: {_describe_event(current)}" if current else "CURRENT EVENT: none"
        )
        recent = context.get("recent_events") or []
        if recent:
            lines.append(
                "RECENT EVENTS (last 3h): "
                + "; ".join(_describe_event(ev) for ev in recent)
            )
        else:
            lines.append("RECENT EVENTS (last 3h): none")
        upcoming = context.get("next_event")
        lines.append(
            f"NEXT EVENT: {_describe_event(upcoming)}" if upcoming else "NEXT EVENT: none"
        )

    lines.append(
        "RPE LOGGED TODAY: " + ("yes" if context.get("rpe_logged_today") else "no")
    )

    injuries = context.get("active_injuries") or []
    if injuries:
        described = []
        for injury in injuries:
            text = _describe_side_part(injury.get("side"), injury.get("body_part"))
            if injury.get("latest_pain") is not None:
                text += f" pain {injury['latest_pain']}/10 ({injury['latest_pain_date']})"
            described.append(text)
        lines.append("ACTIVE INJURIES: " + "; ".join(described))
    else:
        lines.append("ACTIVE INJURIES: none")

    return "\n".join(lines)


# ----------------------------------------------------------------------------------
# confirmation policy


def needs_confirmation(intent: str, confidence: str) -> bool:
    """The risk gradient as a pure decision table.

    CREATE_EVENT and CREATE_TASK write outside this system (iCloud, Notion),
    so they always confirm regardless of confidence: an event on the wrong day
    is worse than one extra tap. Local log writes confirm only on low
    confidence because they are cheap to correct. UNCLEAR is by definition a
    question. QUESTION and CHAT write nothing, so nothing to confirm.
    """
    if intent in _ALWAYS_CONFIRM:
        return True
    if intent in _LOG_INTENTS:
        return confidence == "low"
    return False


# ----------------------------------------------------------------------------------
# corrections (deterministic, no model)

_CORRECTION_OPENER_RE = re.compile(
    r"^\s*(?:no|nope|wrong|actually|correction|i meant|that was)\b",
    re.IGNORECASE,
)

_NUMBER_RE = re.compile(r"\b(?P<value>10|\d(?:\.\d)?)\b")

# Verbs that mean "this is a fresh report", not "fix the last one". A message
# with one of these is a new log even behind a correction opener: "actually my
# left knee hurts 3/10" is a new knee reading, not a correction of an earlier
# entry. Kept broad on purpose; a false negative here (missing a correction)
# just creates a new entry, while a false positive amends the wrong data.
_REPORT_VERB_RE = re.compile(
    r"\b(?:hurts?|hurting|feels?|felt|feeling|sore|aches?|aching|"
    r"tweaked|strained|sprained|pulled|rolled|jammed|tore|"
    r"bothering|tightness|stiff)\b",
    re.IGNORECASE,
)

# An explicit old-value reference, as in "8 not 6". A bare-number correction
# with no such anchor and more than a couple of words ("that was brutal, like
# an 8") is far likelier to be a new session the classifier should see with
# calendar context than a silent overwrite of this morning's RPE.
_OLD_VALUE_REF_RE = re.compile(r"\bnot\s+\d", re.IGNORECASE)

# Reuse training.py's vocabulary so the correction path recognizes exactly the
# same body parts and side words the parser does. Longest first for the same
# first-alternative-wins reason as training._PAIN_RE.
_SIDE_ALT = "|".join(
    re.escape(side) for side in sorted(training.SIDES, key=len, reverse=True)
)
_PART_ALT = "|".join(re.escape(part) for part in training.BODY_PARTS)
_CORR_PART_RE = re.compile(
    rf"\b(?:(?P<side>{_SIDE_ALT})\s+)?(?P<part>{_PART_ALT})s?\b", re.IGNORECASE
)
_CORR_SIDE_RE = re.compile(rf"\b(?P<side>{_SIDE_ALT})\b", re.IGNORECASE)


def detect_correction(text: str) -> dict | None:
    """A deterministic read of 'no, that was 8 not 6' style messages.

    Returns {"kind": "rpe_or_pain", "new_value": n} for a bare number,
    {"kind": "body_part", "body_part": ..., "side": ...} for a body part or
    side mention (with "new_value" added when a number rides along), or None.

    This runs BEFORE classification because it is free, and a small model
    shown "no, 8" plus context is exactly where confabulation starts. The
    caller only treats the result as a correction when an amendable entry
    from today actually exists; otherwise the text falls through to normal
    classification.
    """
    if not _CORRECTION_OPENER_RE.search(text):
        return None

    # A report verb means this is a new entry wearing a correction opener, not
    # an amendment. Fall through to classification so it becomes its own row.
    if _REPORT_VERB_RE.search(text):
        return None

    number = _NUMBER_RE.search(text)
    value: float | int | None = None
    if number:
        parsed = float(number.group("value"))
        value = int(parsed) if parsed == int(parsed) else parsed

    part = _CORR_PART_RE.search(text)
    if part:
        side_word = part.group("side")
        result: dict[str, Any] = {
            "kind": "body_part",
            "body_part": part.group("part").lower(),
            "side": training.SIDES.get(side_word.lower()) if side_word else None,
        }
        if value is not None:
            result["new_value"] = value
        return result

    if value is not None:
        # Bare number, no body part. Only a correction when it either anchors to
        # the old value ("8 not 6") or is terse enough to be nothing but the
        # fix ("actually 7.5"). A longer number-bearing sentence goes to the
        # classifier, which can read calendar context the regex cannot.
        if _OLD_VALUE_REF_RE.search(text) or len(text.split()) <= 3:
            return {"kind": "rpe_or_pain", "new_value": value}
        return None

    side = _CORR_SIDE_RE.search(text)
    if side:
        # "no, it was the left one": side only, body part carried over from
        # today's existing pain entry by amend_latest_pain.
        return {
            "kind": "body_part",
            "body_part": None,
            "side": training.SIDES.get(side.group("side").lower()),
        }

    return None


def _latest_session_today() -> dict | None:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE date = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (_local_today(),),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _latest_pain_today() -> dict | None:
    conn = db.connect()
    try:
        row = conn.execute(
            """
            SELECT l.id AS log_id, l.pain_0_10, i.body_part, i.side
              FROM injury_log l JOIN injuries i ON i.id = l.injury_id
             WHERE l.date = ?
             ORDER BY l.id DESC LIMIT 1
            """,
            (_local_today(),),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _apply_correction(chat_id: int, corr: dict, text: str, entry_id: int) -> str | None:
    """Apply a detected correction to today's data, or return None to fall
    through to normal classification when nothing amendable exists today."""

    def _record(extracted: dict, status: str) -> None:
        db.record_parse(
            entry_id,
            {
                "intent": "LOG_PAIN" if corr["kind"] == "body_part" else "LOG_RPE",
                "confidence": "high",
                "tier": 0,
                "extracted": extracted,
                "reasoning": "deterministic correction",
            },
            status,
            "simple",
        )

    if corr["kind"] == "body_part":
        if _latest_pain_today() is None:
            return None
        new_value = corr.get("new_value")
        new_score = (
            int(new_value)
            if isinstance(new_value, (int, float))
            and float(new_value).is_integer()
            and 0 <= new_value <= 10
            else None
        )
        result = training.amend_latest_pain(
            new_score=new_score,
            new_body_part=corr.get("body_part"),
            new_side=corr.get("side"),
        )
        if result is None:
            return None
        old = _describe_side_part(result["old_side"], result["old_body_part"])
        new = _describe_side_part(result["new_side"], result["new_body_part"])
        lines = [f"corrected: pain entry is now {new} (was {old})"]
        if result["new_score"] != result["old_score"]:
            lines.append(f"pain score {result['old_score']} -> {result['new_score']}")
        lines.append(f"raw text kept as entry #{entry_id}")
        _record(corr, "parsed")
        return "\n".join(lines)

    # kind == "rpe_or_pain"
    value = corr["new_value"]
    if not 0 <= float(value) <= 10:
        # Out of range for both RPE and pain: not a usable correction, let the
        # classifier see the message instead.
        return None

    session = _latest_session_today()
    pain = _latest_pain_today()

    if session is not None and pain is not None and not float(value).is_integer():
        # A decimal cannot be a pain score (those are integers), so there is no
        # ambiguity to ask about: it is an RPE correction. Amending pain here
        # would truncate the value the message actually stated.
        result = training.amend_latest_session_rpe(value)
        if result is not None:
            old = result["old_rpe"] if result["old_rpe"] is not None else "not set"
            _record(corr, "parsed")
            return (
                f"corrected: {result['type']} session RPE {old} -> {result['new_rpe']}\n"
                f"raw text kept as entry #{entry_id}"
            )

    if session is not None and pain is not None:
        # Both a session RPE and a pain reading exist today and a bare number
        # fits either. Ask rather than guess: an RPE written into pain history
        # is exactly the silent data corruption this project exists to avoid.
        question = (
            f"Both a session and a pain reading were logged today. "
            f"Change which one to {value}?\n"
            f"1. session RPE (currently {session['rpe']})\n"
            f"2. pain score (currently {pain['pain_0_10']}, "
            f"{_describe_side_part(pain['side'], pain['body_part'])})"
        )
        _PENDING.open(
            chat_id,
            question=question,
            kind="choice",
            options=["session RPE", "pain score"],
            raw_text=text,
            entry_id=entry_id,
            intent="CORRECTION",
            extracted={"new_value": value},
        )
        _record(corr, "unparsed")
        return question

    if session is not None:
        result = training.amend_latest_session_rpe(value)
        if result is None:
            return None
        old = result["old_rpe"] if result["old_rpe"] is not None else "not set"
        _record(corr, "parsed")
        return (
            f"corrected: {result['type']} session RPE {old} -> {result['new_rpe']}\n"
            f"raw text kept as entry #{entry_id}"
        )

    if pain is not None:
        if not float(value).is_integer():
            # Pain scores are integers; a decimal here is almost certainly an
            # RPE with no session to attach to. Fall through.
            return None
        result = training.amend_latest_pain(new_score=int(value))
        if result is None:
            return None
        _record(corr, "parsed")
        part = _describe_side_part(result["new_side"], result["new_body_part"])
        return (
            f"corrected: {part} pain {result['old_score']} -> {result['new_score']}\n"
            f"raw text kept as entry #{entry_id}"
        )

    return None


# ----------------------------------------------------------------------------------
# field merging and validation


_ANY_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _numbers_in(text: str) -> set[float]:
    """Every numeric literal in a message, for extraction provenance checks."""
    return {float(match) for match in _ANY_NUMBER_RE.findall(text)}


def _as_number(value: Any) -> int | float | None:
    """Best-effort numeric coercion; the model sometimes returns strings."""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return int(parsed) if parsed == int(parsed) else parsed


def _normalize_side(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return training.SIDES.get(value.lower(), value.lower())


def _merge_pain_fields(extracted: dict, raw: str) -> dict:
    """Classifier fields for a pain log, backfilled from the regex parser.

    The model is better at intent, the regex is better at digits, so any
    missing field falls back to training.parse_entry's reading of the raw
    text before anything is asked of the user.
    """
    body_part = extracted.get("body_part")
    side = _normalize_side(extracted.get("side"))
    score = _as_number(extracted.get("pain_0_10"))

    if body_part is None or score is None or side is None:
        parsed = training.parse_entry(raw)
        readings = parsed.get("pain") or []
        if readings:
            first = readings[0]
            body_part = body_part or first.get("body_part")
            side = side or first.get("side")
            score = score if score is not None else first.get("pain_0_10")

    return {
        "body_part": body_part.lower() if isinstance(body_part, str) else None,
        "side": side,
        "pain_0_10": score,
    }


def _session_type_from_event(context: dict) -> str | None:
    """Map a current or just-ended calendar event title to a session type.

    'Team Lift' obviously maps to lift; anything without a SESSION_TYPES
    keyword in the title maps to nothing and the caller falls back to 'other'.
    """
    candidates = [context.get("current_event")]
    candidates.extend(reversed(context.get("recent_events") or []))
    for event in candidates:
        if not event:
            continue
        title = event.get("summary", "").lower()
        for name, keywords in training.SESSION_TYPES.items():
            if any(re.search(rf"\b{re.escape(k)}\b", title) for k in keywords):
                return name
    return None


def _merge_rpe_fields(extracted: dict, raw: str, context: dict) -> dict:
    rpe = _as_number(extracted.get("rpe"))
    session_type = extracted.get("session_type")
    duration = _as_number(extracted.get("duration_min"))

    if rpe is None or duration is None or session_type is None:
        parsed = training.parse_entry(raw)
        sessions = parsed.get("sessions") or []
        if sessions:
            first = sessions[0]
            rpe = rpe if rpe is not None else first.get("rpe")
            duration = duration if duration is not None else first.get("duration_min")
            # The regex reports "other" when it saw no activity word; only a
            # real type is worth taking from it.
            if session_type is None and first.get("type") not in (None, "other"):
                session_type = first.get("type")

    if isinstance(session_type, str):
        session_type = session_type.lower()
    if session_type not in training.SESSION_TYPES:
        session_type = None
    if session_type is None:
        session_type = _session_type_from_event(context) or "other"

    # Same provenance rule the score gets: a duration the model produced that is
    # nowhere in the message is invention, and one past the session-length cap is
    # a weight or a typo. Drop it rather than feed a bad number into the load
    # sums. Dropping (not blocking) is right because a missing duration still
    # leaves a usable RPE log.
    if duration is not None and (
        duration <= 0
        or duration > training.MAX_SESSION_MINUTES
        or float(duration) not in _numbers_in(raw)
    ):
        logging.warning("router: dropping unverifiable duration %s min", duration)
        duration = None

    return {
        "rpe": rpe,
        "session_type": session_type,
        "duration_min": int(duration) if duration is not None else None,
    }


def _merge_part_fields(extracted: dict, raw: str, action: str) -> dict:
    """body_part/side for LOG_INJURY or RESOLVE_INJURY, regex backfilled."""
    body_part = extracted.get("body_part")
    side = _normalize_side(extracted.get("side"))
    description = extracted.get("description")

    if body_part is None:
        parsed = training.parse_entry(raw)
        for injury in parsed.get("injuries") or []:
            if injury.get("action") == action:
                body_part = injury.get("body_part")
                side = side or injury.get("side")
                description = description or injury.get("description")
                break

    return {
        "body_part": body_part.lower() if isinstance(body_part, str) else None,
        "side": side,
        "description": description,
    }


# ----------------------------------------------------------------------------------
# writing handlers


def _write_log_intent(intent: str, fields: dict) -> list[str]:
    """Perform the actual local write for a log intent. Returns reply lines."""
    if intent == "LOG_PAIN":
        # round, not int(): a 3.5 from the model is a 4, not a truncated 3. Pain
        # is stored as an integer 0-10, so the value has to land on one, but it
        # should land on the nearest one rather than always downward.
        score = int(round(float(fields["pain_0_10"])))
        training.log_pain(fields["body_part"], score, fields.get("side"))
        return [
            f"pain: {_describe_side_part(fields.get('side'), fields['body_part'])} "
            f"{score}/10"
        ]

    if intent == "LOG_RPE":
        latest = _latest_session_today()
        # Only complete an existing RPE-less row when it is plausibly the SAME
        # session: same type, or a new message that named no type of its own
        # ("other"). A lift with no RPE followed by "court felt like a 7" is two
        # sessions, not one; attaching the 7 to the lift would fabricate history.
        if (
            latest is not None
            and latest["rpe"] is None
            and fields["session_type"] in (latest["type"], "other")
        ):
            # An RPE arriving after a session row with no RPE (e.g. WHOOP-free
            # manual log, or an earlier partial parse) completes that row
            # instead of creating a duplicate session.
            amended = training.amend_latest_session_rpe(fields["rpe"])
            if amended is not None:
                return [
                    f"RPE {amended['new_rpe']} set on today's "
                    f"{amended['type']} session"
                ]
        training.log_session(
            fields["session_type"],
            duration_min=fields.get("duration_min"),
            rpe=fields["rpe"],
        )
        parts = [fields["session_type"]]
        if fields.get("duration_min") is not None:
            parts.append(f"{fields['duration_min']} min")
        parts.append(f"RPE {fields['rpe']}")
        return ["session: " + " ".join(str(p) for p in parts)]

    if intent == "LOG_INJURY":
        training.open_injury(
            fields["body_part"],
            fields.get("side"),
            fields.get("description") or "",
        )
        return [
            f"injury opened: {_describe_side_part(fields.get('side'), fields['body_part'])}"
        ]

    if intent == "RESOLVE_INJURY":
        described = _describe_side_part(fields.get("side"), fields["body_part"])
        if training.resolve_injury_by_part(fields["body_part"], fields.get("side")):
            return [f"injury resolved: {described}"]
        return [f"no active injury matching {described} to resolve"]

    raise ValueError(f"not a log intent: {intent!r}")


def _log_reply(lines: list[str], entry_id: int) -> str:
    """Same shape as training.format_parse_reply: what happened, then the id."""
    return "logged:\n" + "\n".join(f"- {line}" for line in lines) + (
        f"\nraw text kept as entry #{entry_id}"
    )


def _confirm_question_for(intent: str, fields: dict) -> str:
    if intent == "LOG_PAIN":
        part = _describe_side_part(fields.get("side"), fields["body_part"])
        return f"Log pain: {part} {int(round(float(fields['pain_0_10'])))}/10? (yes/no)"
    if intent == "LOG_RPE":
        duration = (
            f" {fields['duration_min']} min" if fields.get("duration_min") else ""
        )
        return (
            f"Log session: {fields['session_type']}{duration} "
            f"RPE {fields['rpe']}? (yes/no)"
        )
    if intent == "LOG_INJURY":
        part = _describe_side_part(fields.get("side"), fields["body_part"])
        return f"Open a new injury: {part}? (yes/no)"
    if intent == "RESOLVE_INJURY":
        part = _describe_side_part(fields.get("side"), fields["body_part"])
        return f"Mark {part} resolved? (yes/no)"
    raise ValueError(f"no confirm question for intent {intent!r}")


# ----------------------------------------------------------------------------------
# create flows (external writes, always confirmed)


def _start_event_flow(
    chat_id: int,
    text: str,
    source_entry_id: int,
    ctx_block: str,
) -> tuple[str, str]:
    """Extract event details and open the confirm question.

    Returns (reply, parse_status). Never writes to the calendar directly;
    the write happens only in _resolve_pending after an explicit yes.
    """
    try:
        event = llm.extract_event(text, ctx_block)
    except requests.RequestException:
        logging.warning("router: extract_event unavailable")
        return (
            "the model is unavailable, so I could not read the event details. "
            f"raw text kept as entry #{source_entry_id}, try again later.",
            "error",
        )

    raw_title = event.get("title")
    title = (str(raw_title).strip() if raw_title else "") or text.strip()
    try:
        start, end = calendar_source.resolve_time(
            event.get("date"), event.get("start_time"), event.get("end_time")
        )
    except ValueError:
        # A confirm question would be wrong here (there is nothing to say yes
        # to); ask the one specific thing that is missing instead.
        return (
            f"When is '{title}'? I need at least a start time, "
            "like 'tomorrow 3pm' or 'friday 15:00-16:30'.",
            "unparsed",
        )

    location = event.get("location")
    where = f" at {location}" if location else ""
    question = (
        f"Create event '{title}' {start.strftime('%Y-%m-%d')} "
        f"{_fmt_time(start)}-{_fmt_time(end)}{where}? (yes/no)"
    )
    _PENDING.open(
        chat_id,
        question=question,
        kind="confirm",
        options=[],
        raw_text=text,
        entry_id=source_entry_id,
        intent="CREATE_EVENT",
        extracted={
            "title": title,
            "location": location,
            "start_iso": start.isoformat(),
            "end_iso": end.isoformat(),
        },
    )
    return question, "unparsed"


# The Type property's options in the Coursework schema (CLAUDE.md). Unlike
# Course, which grows in the Notion UI and must not be hardcoded, Type is a
# fixed enum, so a value outside it is a model mistake. Notion silently creates
# a new select option for any unknown value, turning a typo into a permanent UI
# artifact, so an unrecognized type is dropped rather than sent.
_TASK_TYPES = frozenset({"pset", "exam", "lab", "reading", "project", "quiz"})
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _start_task_flow(
    chat_id: int, text: str, source_entry_id: int, extracted: dict
) -> tuple[str, str]:
    """Build the task from extracted fields and open the confirm question.

    Fields the model produced are validated here, before the confirm question
    and long before Notion: a title coerced to text (a crafted result could make
    it a dict), a type checked against the fixed enum, and a due date checked for
    ISO shape. A malformed value is dropped so the task still gets created rather
    than failing after the user has already tapped yes.
    """
    raw_title = extracted.get("title")
    title = (str(raw_title).strip() if raw_title else "") or text.strip()

    due_date = extracted.get("due_date")
    if due_date is not None and not _ISO_DATE_RE.match(str(due_date)):
        logging.warning("router: dropping malformed task due_date %r", due_date)
        due_date = None

    course = extracted.get("course")

    task_type = extracted.get("task_type")
    if task_type is not None:
        task_type = str(task_type).lower()
        if task_type not in _TASK_TYPES:
            logging.warning("router: dropping unknown task_type %r", task_type)
            task_type = None

    details = []
    if due_date:
        details.append(f"due {due_date}")
    if course:
        details.append(str(course))
    if task_type:
        details.append(str(task_type))
    suffix = f" ({', '.join(details)})" if details else ""
    question = f"Create Notion task '{title}'{suffix}? (yes/no)"

    _PENDING.open(
        chat_id,
        question=question,
        kind="confirm",
        options=[],
        raw_text=text,
        entry_id=source_entry_id,
        intent="CREATE_TASK",
        extracted={
            "title": title,
            "due_date": due_date,
            "course": course,
            "task_type": task_type,
        },
    )
    return question, "unparsed"


# ----------------------------------------------------------------------------------
# pending answers


def _interpret_answer(pending: Pending, text: str) -> bool | int | None:
    """Read a message as the answer to the open question, or None.

    None means "this is not an answer": the caller drops the pending item and
    routes the message through normal classification instead.
    """
    token = text.strip().lower().strip(".!?,")

    if pending.kind == "confirm":
        # First-token match, not whole-string: "yes do it" and "no thanks" are
        # clear answers, and "no, right shoulder 4" is a rejection of THIS
        # question (not a standalone correction of some earlier entry). Reading
        # the leading word keeps a rejection-with-restatement from leaking into
        # the correction path, where it would rewrite unrelated same-day data.
        words = token.split()
        first = words[0].strip(".,!?;:") if words else ""
        if first in _YES_WORDS:
            return True
        if first in _NO_WORDS:
            return False
        return None

    # choice: a number in range, or a case-insensitive word match against
    # exactly one option label.
    if token.isdigit():
        index = int(token)
        if 1 <= index <= len(pending.options):
            return index - 1
        return None

    matches = []
    answer_words = set(re.findall(r"[a-z0-9]+", token))
    if not answer_words:
        return None
    for index, label in enumerate(pending.options):
        label_words = set(re.findall(r"[a-z0-9]+", label.lower()))
        if token == label.lower() or answer_words <= label_words:
            matches.append(index)
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_pending(
    chat_id: int, pending: Pending, answer: bool | int, entry_id: int
) -> str:
    """Carry out (or cancel) whatever the pending question was about.

    entry_id is the log_entries row of the ANSWER message, which the core
    principle already stored; record_parse marks it method router-reply.
    """
    record = {
        "intent": pending.intent,
        "confidence": "high",
        "tier": 0,
        "extracted": pending.extracted,
        "reasoning": "reply to pending question",
    }

    def _mark(status: str) -> None:
        db.record_parse(entry_id, record, status, "router-reply")

    if pending.kind == "confirm":
        if answer is False:
            _mark("parsed")
            if pending.intent in ("CREATE_EVENT", "CREATE_TASK"):
                return (
                    "ok, not created. "
                    f"raw text kept as entry #{pending.entry_id}"
                )
            return f"ok, not logged. raw text kept as entry #{pending.entry_id}"

        if pending.intent == "CREATE_EVENT":
            extracted = pending.extracted
            try:
                uid = calendar_source.create_event(
                    extracted["title"],
                    datetime.fromisoformat(extracted["start_iso"]),
                    datetime.fromisoformat(extracted["end_iso"]),
                    location=extracted.get("location"),
                )
            except Exception as exc:
                logging.exception("router: create_event failed")
                _mark("error")
                return (
                    f"creating the event failed: {exc}\n"
                    f"raw text kept as entry #{pending.entry_id}"
                )
            _mark("parsed")
            start = datetime.fromisoformat(extracted["start_iso"])
            end = datetime.fromisoformat(extracted["end_iso"])
            return (
                f"created event '{extracted['title']}' "
                f"{start.strftime('%Y-%m-%d')} {_fmt_time(start)}-{_fmt_time(end)} "
                f"(uid {uid})"
            )

        if pending.intent == "CREATE_TASK":
            extracted = pending.extracted
            try:
                task = notion_source.create_task(
                    extracted["title"],
                    due_date=extracted.get("due_date"),
                    course=extracted.get("course"),
                    task_type=extracted.get("task_type"),
                )
            except Exception as exc:
                logging.exception("router: create_task failed")
                _mark("error")
                return (
                    f"creating the task failed: {exc}\n"
                    f"raw text kept as entry #{pending.entry_id}"
                )
            _mark("parsed")
            due = f" due {task['due_date']}" if task.get("due_date") else ""
            return f"created task '{task['title']}'{due}\n{task.get('url', '')}".strip()

        if pending.intent in _LOG_INTENTS:
            lines = _write_log_intent(pending.intent, pending.extracted)
            _mark("parsed")
            return _log_reply(lines, pending.entry_id)

        _mark("parsed")
        return "ok"

    # choice answers
    index = int(answer)

    if pending.intent == "CORRECTION":
        value = pending.extracted["new_value"]
        if index == 0:
            result = training.amend_latest_session_rpe(value)
            if result is None:
                _mark("error")
                return "no session found today anymore; nothing changed"
            _mark("parsed")
            old = result["old_rpe"] if result["old_rpe"] is not None else "not set"
            return f"corrected: {result['type']} session RPE {old} -> {result['new_rpe']}"
        result = training.amend_latest_pain(new_score=int(value))
        if result is None:
            _mark("error")
            return "no pain reading found today anymore; nothing changed"
        _mark("parsed")
        part = _describe_side_part(result["new_side"], result["new_body_part"])
        return f"corrected: {part} pain {result['old_score']} -> {result['new_score']}"

    if pending.intent == "UNCLEAR":
        if index == 0:  # log it as training or pain
            result = training.parse_entry(pending.raw_text)
            applied = training.apply_entry(result)
            # Upgrade the ORIGINAL entry's parse record too, now that we know
            # what it was.
            db.record_parse(
                pending.entry_id, result, result["parse_status"], "simple"
            )
            _mark("parsed")
            return training.format_parse_reply(result, applied, pending.entry_id)
        if index == 1:  # add a calendar event
            _mark("parsed")
            ctx_block = format_context(gather_context())
            reply, _status = _start_event_flow(
                chat_id, pending.raw_text, pending.entry_id, ctx_block
            )
            return reply
        if index == 2:  # create a task
            _mark("parsed")
            reply, _status = _start_task_flow(
                chat_id, pending.raw_text, pending.entry_id, {}
            )
            return reply
        _mark("parsed")
        return f"ok, kept as a note. raw text is entry #{pending.entry_id}"

    _mark("parsed")
    return "ok"


# ----------------------------------------------------------------------------------
# intent dispatch


def _dispatch_intent(
    chat_id: int,
    text: str,
    entry_id: int,
    result: dict,
    context: dict,
    ctx_block: str,
) -> str:
    intent = result.get("intent", "UNCLEAR")
    confidence = result.get("confidence", "low")
    extracted = result.get("extracted") or {}

    record = {
        "intent": intent,
        "confidence": confidence,
        "tier": result.get("tier"),
        "extracted": extracted,
        "reasoning": result.get("reasoning"),
    }

    def _record(status: str) -> None:
        db.record_parse(entry_id, record, status, "router-llm")

    if intent == "QUESTION":
        _record("parsed")
        return (
            "not built yet. Q&A over your history lands in a later phase "
            "(text-to-SQL over the local database)."
        )

    if intent == "CHAT":
        try:
            reply = llm.chat(text, ctx_block)
        except requests.RequestException:
            _record("error")
            return (
                "the model is unavailable for chat right now. "
                f"raw text kept as entry #{entry_id}"
            )
        _record("parsed")
        return reply

    if intent == "CREATE_EVENT":
        reply, status = _start_event_flow(chat_id, text, entry_id, ctx_block)
        _record(status)
        return reply

    if intent == "CREATE_TASK":
        reply, status = _start_task_flow(chat_id, text, entry_id, extracted)
        _record(status)
        return reply

    if intent in _LOG_INTENTS:
        return _dispatch_log_intent(
            chat_id, text, entry_id, intent, confidence, extracted, context, _record
        )

    # UNCLEAR, or an intent this router does not know (defensive: the contract
    # says classify never returns one, but a dispatch table should not crash
    # on a new value either).
    lines = ["I could not tell what that message was."]
    current = context.get("current_event")
    if current:
        lines.append(f"Your calendar shows {_describe_event(current)} right now.")
    lines.append("What should I do with it?")
    lines.extend(
        f"{i + 1}. {option}" for i, option in enumerate(_UNCLEAR_OPTIONS)
    )
    question = "\n".join(lines)
    _PENDING.open(
        chat_id,
        question=question,
        kind="choice",
        options=list(_UNCLEAR_OPTIONS),
        raw_text=text,
        entry_id=entry_id,
        intent="UNCLEAR",
        extracted=extracted,
    )
    _record("unparsed")
    return question


def _dispatch_log_intent(
    chat_id: int,
    text: str,
    entry_id: int,
    intent: str,
    confidence: str,
    extracted: dict,
    context: dict,
    _record: Callable[[str], None],
) -> str:
    """Merge fields, validate, gate on confirmation, then write."""
    if intent == "LOG_PAIN":
        fields = _merge_pain_fields(extracted, text)
        if fields["body_part"] is None or fields["pain_0_10"] is None:
            _record("unparsed")
            return (
                "that looks like a pain log but I could not read "
                + ("the body part" if fields["body_part"] is None else "the score")
                + ". Send it like 'left shoulder 4'."
            )
        if not 0 <= float(fields["pain_0_10"]) <= 10:
            # Out-of-range downgrade: never write garbage, ask instead.
            _record("unparsed")
            return (
                f"pain {fields['pain_0_10']} is outside 0-10, so I did not log it. "
                "What was the score?"
            )
    elif intent == "LOG_RPE":
        fields = _merge_rpe_fields(extracted, text, context)
        if fields["rpe"] is None:
            _record("unparsed")
            return "that looks like a session log but I could not read the RPE. What was it (0-10)?"
        if not 0 <= float(fields["rpe"]) <= 10:
            _record("unparsed")
            return (
                f"RPE {fields['rpe']} is outside 0-10, so I did not log it. "
                "What was it?"
            )
    else:  # LOG_INJURY / RESOLVE_INJURY
        action = "open" if intent == "LOG_INJURY" else "resolve"
        fields = _merge_part_fields(extracted, text, action)
        if fields["body_part"] is None:
            _record("unparsed")
            actives = context.get("active_injuries") or []
            if intent == "RESOLVE_INJURY" and actives:
                names = "; ".join(
                    _describe_side_part(i.get("side"), i.get("body_part"))
                    for i in actives
                )
                return f"which injury is resolved? Currently active: {names}"
            return "which body part? Send it like 'tweaked my left ankle'."

    # Anti-confabulation guard: a score the model "extracted" that appears
    # nowhere in the message is not data, it is generation. Seen live: the
    # few-shot example value 8 got copied onto "practice was rough today",
    # which contains no number at all. Every logged claim must trace to the
    # message, so a value with no provenance is downgraded to a confirmation
    # question instead of being written.
    claimed = (
        fields.get("pain_0_10") if intent == "LOG_PAIN"
        else fields.get("rpe") if intent == "LOG_RPE"
        else None
    )
    if claimed is not None and float(claimed) not in _numbers_in(text):
        logging.warning(
            "router: %s value %s not present in message, forcing confirmation",
            intent, claimed,
        )
        confidence = "low"

    if needs_confirmation(intent, confidence):
        question = _confirm_question_for(intent, fields)
        _PENDING.open(
            chat_id,
            question=question,
            kind="confirm",
            options=[],
            raw_text=text,
            entry_id=entry_id,
            intent=intent,
            extracted=fields,
        )
        _record("unparsed")
        return question

    lines = _write_log_intent(intent, fields)
    _record("parsed")
    return _log_reply(lines, entry_id)


# ----------------------------------------------------------------------------------
# fallback and entry point


def _fallback_simple(text: str, entry_id: int) -> str:
    """Model down: the phase 4 regex path, with an honest note in the reply."""
    result = training.parse_entry(text)
    applied = training.apply_entry(result)
    db.record_parse(entry_id, result, result["parse_status"], "simple")
    return (
        "(model unavailable, degraded to simple parsing)\n"
        + training.format_parse_reply(result, applied, entry_id)
    )


def _handle(chat_id: int, text: str, entry_id: int) -> str:
    just_dropped_pending = False
    pending = _PENDING.get(chat_id)
    if pending is not None:
        answer = _interpret_answer(pending, text)
        if answer is not None:
            _PENDING.clear(chat_id)
            return _resolve_pending(chat_id, pending, answer, entry_id)
        # Not an answer: the new message wins, the question is dropped rather
        # than stacked behind it.
        _PENDING.clear(chat_id)
        just_dropped_pending = True
        logging.info(
            "router: dropped pending %s question for chat %s, new message replaces it",
            pending.intent,
            chat_id,
        )

    # The correction path is skipped when a pending was just dropped: a message
    # sent while a question was open is answering or replacing that question,
    # not a freestanding "no, that was 8" about some earlier entry. Running the
    # correction path here is what let a rejected confirm rewrite unrelated
    # same-day data. Send it through classification instead.
    if not just_dropped_pending:
        corr = detect_correction(text)
        if corr is not None:
            reply = _apply_correction(chat_id, corr, text, entry_id)
            if reply is not None:
                return reply
            # Nothing amendable today: not a correction after all.

    context = gather_context()
    ctx_block = format_context(context)

    try:
        result = llm.classify(text, ctx_block)
    except (requests.RequestException, OSError):
        # OSError covers transport failures below requests' own hierarchy
        # (e.g. a refused socket surfacing raw on some paths).
        logging.warning("router: classifier unavailable, using simple parsing")
        return _fallback_simple(text, entry_id)

    return _dispatch_intent(chat_id, text, entry_id, result, context, ctx_block)


def handle_message(chat_id: int, text: str) -> str:
    """Route one inbound message and return the reply. Never raises.

    The raw text hits log_entries before anything else runs; see the module
    docstring. The outermost except exists because an exception escaping into
    python-telegram-bot is swallowed there and looks like the bot ignoring
    you, which is the one failure mode this project treats as unacceptable.
    """
    try:
        entry_id = db.insert_log_entry(text)
    except Exception:
        # The one genuinely bad outcome: the message is NOT stored. Say so
        # plainly instead of pretending.
        logging.exception("router: could not store inbound message")
        return "could not store that message, nothing was written. please resend it."

    try:
        return _handle(chat_id, text, entry_id)
    except Exception:
        logging.exception("router: handling entry #%d failed", entry_id)
        try:
            db.record_parse(entry_id, None, "error", "router-llm")
        except Exception:
            logging.exception("router: could not mark entry #%d as errored", entry_id)
        return (
            "something went wrong handling that, but the raw text is stored "
            f"as entry #{entry_id}. nothing was lost."
        )
