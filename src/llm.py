"""Local inference via Ollama. Phase 7.

Ollama runs natively on Windows and exposes an HTTP API on port 11434. Nothing here
should ever call a hosted model.

The model's entire job in the briefing is ONE sentence: last night's sleep and
recovery against the 14 day baseline. Every number, date, and line of layout is
rendered deterministically in brief.py, so a confabulated figure cannot reach
Telegram. The worst a bad generation can do is cost this one line, and by design
it is omitted rather than replaced with filler when that happens.
"""

import json
import logging
import re

import requests

from src import config

SLEEP_NOTE_SYSTEM = """You compare last night's sleep and recovery to a 14 day baseline.

Rules:
- Exactly one sentence, under 20 words.
- Use only the numbers provided. Never invent numbers.
- No advice, no recommendations, no encouragement.
- Plain text. No markdown, no emoji, no em dashes.
"""

# Generous because the 7 AM run is unattended and the model may need a cold load
# from disk into VRAM first. Interactive callers should not reuse this number.
REQUEST_TIMEOUT_SECONDS = 120

# The prompt is two labeled lines of numbers. A small window means a smaller KV
# cache to allocate on an 8GB card and a faster cold load.
NUM_CTX = 2048

# Low but not zero: the job is restating given facts, not composing. Higher
# temperatures are where invented numbers come from.
TEMPERATURE = 0.3

# The prompt asks for under 20 words. 30 is the omission threshold, not a target:
# a model a third over the limit is drifting into commentary, and no line beats a
# rambling one in a fixed template.
MAX_NOTE_WORDS = 30

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def generate_sleep_note(
    sleep_hours: float,
    recovery: float,
    avg_sleep_hours: float,
    avg_recovery: float,
) -> str | None:
    """One sentence comparing last night to the 14 day baseline, or None.

    Returns None when Ollama is unreachable, times out, errors, returns empty
    text, or runs past MAX_NOTE_WORDS. The caller omits the line entirely;
    there is deliberately no canned fallback sentence, because filler text
    pretending to be the model reads as data and is worse than a missing line.
    """
    prompt = (
        f"LAST NIGHT: slept {sleep_hours:.1f} hours, recovery {recovery:.0f}\n"
        f"14 DAY AVERAGE: slept {avg_sleep_hours:.1f} hours, recovery {avg_recovery:.0f}\n"
        "Write the one sentence comparison."
    )

    try:
        response = requests.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={
                "model": config.OLLAMA_MODEL,
                "system": SLEEP_NOTE_SYSTEM,
                "prompt": prompt,
                # Qwen3 is a hybrid thinking model. With thinking on it spent
                # 605 tokens reasoning its way to a one word answer, and the
                # reasoning text can leak into what gets sent to Telegram.
                "think": False,
                "stream": False,
                "options": {"num_ctx": NUM_CTX, "temperature": TEMPERATURE},
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        text = response.json().get("response") or ""
    except (requests.RequestException, ValueError) as exc:
        # ValueError covers a 2xx with a non-JSON body.
        logging.warning("sleep note: ollama failed: %s", exc)
        return None

    # Collapse all whitespace to single spaces: the note lands in a fixed
    # template as one line, and a stray newline from the model would break it.
    note = " ".join(_strip_think(text).split())

    if not note:
        logging.warning("sleep note: model returned empty text")
        return None

    words = len(note.split())
    if words > MAX_NOTE_WORDS:
        logging.warning(
            "sleep note: %d words (max %d), omitting", words, MAX_NOTE_WORDS
        )
        return None

    logging.info("sleep note: %d words", words)
    return note


def _strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning, closed or not.

    Second layer of defense behind "think": false in the request. Belt and
    suspenders is warranted here because the failure mode is a wall of chain of
    thought arriving on a phone at 7 AM.
    """
    text = _THINK_RE.sub("", text)

    # An unclosed <think> means the model was still reasoning when it ran out of
    # tokens; everything after the tag is reasoning, not briefing.
    unclosed = text.find("<think>")
    if unclosed != -1:
        text = text[:unclosed]

    return text


# ---------------------------------------------------------------------------
# Interactive layer: intent classification, event extraction, chat.
# All three use OLLAMA_MODEL_FAST because a person on a phone is waiting.
# ---------------------------------------------------------------------------

# These differ from REQUEST_TIMEOUT_SECONDS on purpose. The 7 AM sleep note is
# unattended, so 120s absorbs a cold model load from disk into VRAM. Here the
# reply lands on a phone mid-conversation, so the budget is what a person will
# tolerate. Tier 2 gets the most because thinking generates hundreds of tokens
# before the answer; tier 1 is a short forced-JSON generation and should be
# quick or not at all.
TIER1_TIMEOUT_SECONDS = 15
TIER2_TIMEOUT_SECONDS = 45
EXTRACT_EVENT_TIMEOUT_SECONDS = 20
CHAT_TIMEOUT_SECONDS = 30

# Near zero for classification and extraction: the job is reading fields out of
# text, and temperature is where invented fields come from. Chat composes so it
# gets more, but not much more: at 0.7 the live model cross-wired a pain score
# into an "RPE of 3/10" in a reply, and grounded beats lively here.
CLASSIFY_TEMPERATURE = 0.1
CHAT_TEMPERATURE = 0.4

INTENTS = frozenset(
    {
        "LOG_PAIN",
        "LOG_RPE",
        "LOG_INJURY",
        "RESOLVE_INJURY",
        "CREATE_EVENT",
        "CREATE_TASK",
        "QUESTION",
        "CHAT",
        "UNCLEAR",
    }
)

_VALID_CONFIDENCE = {"high", "low"}

# Shared between both tiers so the intent definitions cannot drift apart.
_INTENT_GUIDE = """You classify one incoming Telegram message from Veer, a college athlete, into exactly one intent.

Intents:
- LOG_PAIN: reporting pain or soreness, usually a body part plus a 0-10 score
- LOG_RPE: reporting how hard a training session felt (RPE 0-10), maybe with session type or duration
- LOG_INJURY: a new injury or tweak just happened
- RESOLVE_INJURY: an existing injury is healed, resolved, or cleared
- CREATE_EVENT: add something to the calendar
- CREATE_TASK: add coursework or a to-do (pset, lab, exam, reading, project, quiz)
- QUESTION: asking about his own logged history or data
- CHAT: small talk or conversation with no action to take
- UNCLEAR: cannot tell what is meant, or a bare number with no referent

Output a JSON object with exactly these keys:
  intent: one of the nine intents above
  confidence: "high" or "low"
  reasoning: one short sentence
  extracted: object, shape depends on intent (use null for anything missing):
    LOG_PAIN: {"body_part": str, "side": str, "pain_0_10": int}
    LOG_RPE: {"rpe": number, "session_type": str, "duration_min": number}
    LOG_INJURY: {"body_part": str, "side": str, "description": str}
    RESOLVE_INJURY: {"body_part": str, "side": str}
    CREATE_TASK: {"title": str, "due_date": "YYYY-MM-DD", "course": str, "task_type": str}
    all other intents: {}

The context block matters: what is on the calendar right now, what just ended, what is next, and whether RPE is already logged today can change the answer for a vague message.

HARD RULE for extracted: every number must be copied verbatim from the message. If the message contains no number, extracted must contain no number. Never take a number from the context block or from these instructions."""

_TIER1_SYSTEM = _INTENT_GUIDE + """

Respond with ONLY the JSON object, no other text.

Examples:

MESSAGE: right shoulder 3
{"intent": "LOG_PAIN", "confidence": "high", "reasoning": "body part with 0-10 score", "extracted": {"body_part": "shoulder", "side": "right", "pain_0_10": 3}}

MESSAGE: court 90 rpe 6
{"intent": "LOG_RPE", "confidence": "high", "reasoning": "session type, duration, explicit rpe", "extracted": {"rpe": 6, "session_type": "court", "duration_min": 90}}

MESSAGE: lifted 60 min rpe 8
{"intent": "LOG_RPE", "confidence": "high", "reasoning": "lift with duration and rpe", "extracted": {"rpe": 8, "session_type": "lift", "duration_min": 60}}

MESSAGE: tweaked my left ankle
{"intent": "LOG_INJURY", "confidence": "high", "reasoning": "new tweak on a body part", "extracted": {"body_part": "ankle", "side": "left", "description": "tweaked my left ankle"}}

MESSAGE: right shoulder resolved
{"intent": "RESOLVE_INJURY", "confidence": "high", "reasoning": "existing injury marked resolved", "extracted": {"body_part": "shoulder", "side": "right"}}

MESSAGE: add lift tomorrow 3pm
{"intent": "CREATE_EVENT", "confidence": "high", "reasoning": "calendar add request", "extracted": {}}

CONTEXT: today is 2026-07-22 Wednesday
MESSAGE: add circuits pset due friday
{"intent": "CREATE_TASK", "confidence": "high", "reasoning": "coursework with a due date", "extracted": {"title": "circuits pset", "due_date": "2026-07-24", "course": null, "task_type": "pset"}}

MESSAGE: what was my recovery last tuesday
{"intent": "QUESTION", "confidence": "high", "reasoning": "asks about logged history", "extracted": {}}

MESSAGE: lol nice
{"intent": "CHAT", "confidence": "high", "reasoning": "conversational, nothing to log", "extracted": {}}

MESSAGE: man that practice was rough today
{"intent": "CHAT", "confidence": "high", "reasoning": "venting about a session but gives no number and asks for nothing", "extracted": {}}

CONTEXT: calendar now: Lift 15:00-16:30 (in progress). rpe logged today: no
MESSAGE: that was brutal, 8
{"intent": "LOG_RPE", "confidence": "high", "reasoning": "a lift is on now and rpe is not logged, so 8 is the rpe", "extracted": {"rpe": 8, "session_type": "lift", "duration_min": null}}

CONTEXT: calendar now: nothing. rpe logged today: yes
MESSAGE: that was brutal, 8
{"intent": "UNCLEAR", "confidence": "low", "reasoning": "8 has no referent, rpe already logged and nothing on the calendar", "extracted": {}}

MESSAGE: 8
{"intent": "UNCLEAR", "confidence": "low", "reasoning": "bare number with no referent", "extracted": {}}"""

_TIER2_SYSTEM = _INTENT_GUIDE + """

Think it through before answering. Weigh the calendar context explicitly: what is
happening right now, what just ended, what is coming next, and whether RPE is
already logged today. A vague message like "that was brutal, 8" is LOG_RPE when a
training session is on or just ended and RPE is unlogged, and UNCLEAR when nothing
fits. Prefer UNCLEAR with low confidence over guessing.

After reasoning, your final output must be ONLY the JSON object described above."""


def _unclear(tier: int) -> dict:
    """The contract's fallback for anything the model botched."""
    return {
        "intent": "UNCLEAR",
        "confidence": "low",
        "reasoning": "unparseable model output",
        "extracted": {},
        "tier": tier,
    }


def _parse_json_object(raw: str) -> dict | None:
    """json.loads with one retry on the outermost {...} slice.

    Tier 2 runs without Ollama's format constraint (see _classify_once), so the
    model may wrap the JSON in prose. Grabbing first-{ to last-} recovers that
    case without trying to be a real parser.
    """
    for candidate in (raw, raw[raw.find("{") : raw.rfind("}") + 1]):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _validate_classification(raw: str, tier: int) -> dict:
    """Coerce model output into the contract shape. Never raises."""
    data = _parse_json_object(raw)
    if data is None:
        return _unclear(tier)

    intent = data.get("intent")
    if intent not in INTENTS:
        return _unclear(tier)

    confidence = data.get("confidence")
    if confidence not in _VALID_CONFIDENCE:
        confidence = "low"

    extracted = data.get("extracted")
    if not isinstance(extracted, dict):
        extracted = {}

    return {
        "intent": intent,
        "confidence": confidence,
        "reasoning": str(data.get("reasoning") or ""),
        "extracted": extracted,
        "tier": tier,
    }


def _classify_once(text: str, context: str, tier: int) -> dict:
    """One classification call. Raises requests.RequestException on transport
    or HTTP failure; every other failure mode degrades to the UNCLEAR dict."""
    think = tier == 2
    payload = {
        "model": config.OLLAMA_MODEL_FAST,
        "system": _TIER2_SYSTEM if think else _TIER1_SYSTEM,
        "prompt": f"CONTEXT:\n{context or '(none)'}\n\nMESSAGE: {text}",
        "think": think,
        "stream": False,
        "options": {"num_ctx": NUM_CTX, "temperature": CLASSIFY_TEMPERATURE},
    }
    # Tier 1 hard-constrains output to JSON. Tier 2 deliberately does not:
    # combining "format": "json" with thinking is unreliable across Ollama
    # versions (some constrain the reasoning stream itself), so tier 2 relies
    # on the prompt plus _strip_think plus the {...} rescue in
    # _parse_json_object.
    if not think:
        payload["format"] = "json"

    response = requests.post(
        f"{config.OLLAMA_URL}/api/generate",
        json=payload,
        timeout=TIER2_TIMEOUT_SECONDS if think else TIER1_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError:
        # A 200 with a non-JSON body from Ollama itself. The contract says this
        # is unparseable model output, not a transport failure, so no raise.
        logging.warning("classify: ollama returned non-JSON body")
        return _unclear(tier)

    raw = body.get("response") or ""
    if think:
        # With think=true recent Ollama puts reasoning in a separate "thinking"
        # field, but older builds inline <think> tags in "response". Strip both
        # ways; we only ever read "response".
        raw = _strip_think(raw)
    return _validate_classification(raw, tier)


def classify(text: str, context: str) -> dict:
    """Two-tier intent classification.

    Tier 1 is a fast no-think call. Anything low-confidence or UNCLEAR escalates
    to tier 2, which thinks before answering. Raises requests.RequestException
    only when tier 1 cannot be reached at all; the router catches that and falls
    back to the regex parser.
    """
    result = _classify_once(text, context, tier=1)

    if result["confidence"] == "high" and result["intent"] != "UNCLEAR":
        logging.info("classify: tier 1 %s %s", result["confidence"], result["intent"])
        return result

    logging.info(
        "classify: escalated to tier 2, tier 1 said %s %s (%s)",
        result["confidence"],
        result["intent"],
        result["reasoning"],
    )
    try:
        tier2 = _classify_once(text, context, tier=2)
    except requests.RequestException as exc:
        # Tier 1 already produced a valid (if weak) answer, which beats making
        # the router discard everything for the regex fallback just because the
        # slower thinking call timed out.
        logging.warning("classify: tier 2 failed (%s), keeping tier 1 result", exc)
        return result

    logging.info("classify: tier 2 %s %s", tier2["confidence"], tier2["intent"])
    return tier2


_EXTRACT_EVENT_SYSTEM = """You extract calendar event fields from one message from Veer. Respond with ONLY a JSON object with keys: title, date, start_time, end_time, location.

Rules:
- title: short event name, always present.
- date: "YYYY-MM-DD" or null. The context gives the current date and weekday. Resolve relative words like "tomorrow", "friday", or "sunday" against it and return the literal computed date. Absolute dates in the text pass through as written.
- start_time and end_time: 24-hour "HH:MM" or null.
- location: string or null. Never invent one.

Examples:

CONTEXT: now: 2026-07-24 15:00 Friday
MESSAGE: add lift tomorrow 3pm
{"title": "lift", "date": "2026-07-25", "start_time": "15:00", "end_time": null, "location": null}

CONTEXT: now: 2026-07-22 09:00 Wednesday
MESSAGE: dinner with team friday 7pm at marlowe
{"title": "dinner with team", "date": "2026-07-24", "start_time": "19:00", "end_time": null, "location": "marlowe"}
(friday is 2 days after Wednesday the 22nd, so 22 + 2 = 2026-07-24)

CONTEXT: now: 2026-07-24 15:00 Friday
MESSAGE: block out 2-4 sunday for the pset
{"title": "pset", "date": "2026-07-26", "start_time": "14:00", "end_time": "16:00", "location": null}

CONTEXT: now: 2026-07-24 15:00 Friday
MESSAGE: pt appointment aug 3 9am
{"title": "pt appointment", "date": "2026-08-03", "start_time": "09:00", "end_time": null, "location": null}"""

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _clean_field(value: object, pattern: re.Pattern | None = None) -> str | None:
    """Coerce a model-produced field to str or None, format-checked if asked.

    Nulling a malformed date or time here means the router's resolve_time sees
    either contract-shaped input or None, never "3pm"."""
    if value is None or not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if pattern is not None and not pattern.match(value):
        return None
    return value


def extract_event(text: str, context: str) -> dict:
    """Pull calendar event fields out of free text.

    Raises requests.RequestException on transport/HTTP failure or a broken
    Ollama envelope. Bad model output degrades to the raw text as title with
    everything else None, which downstream turns into a "when?" follow-up
    rather than a crash.
    """
    response = requests.post(
        f"{config.OLLAMA_URL}/api/generate",
        json={
            "model": config.OLLAMA_MODEL_FAST,
            "system": _EXTRACT_EVENT_SYSTEM,
            "prompt": f"CONTEXT:\n{context or '(none)'}\n\nMESSAGE: {text}",
            "think": False,
            "format": "json",
            "stream": False,
            "options": {"num_ctx": NUM_CTX, "temperature": CLASSIFY_TEMPERATURE},
        },
        timeout=EXTRACT_EVENT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    try:
        raw = response.json().get("response") or ""
    except ValueError as exc:
        # Ollama itself sent garbage; that is a transport-level failure to the
        # caller, unlike the model writing bad JSON below.
        raise requests.RequestException("ollama returned non-JSON body") from exc

    data = _parse_json_object(raw)
    if data is None:
        logging.warning("extract_event: unparseable model output")
        data = {}

    title = _clean_field(data.get("title"))
    return {
        "title": title if title is not None else text.strip(),
        "date": _clean_field(data.get("date"), _DATE_RE),
        "start_time": _clean_field(data.get("start_time"), _TIME_RE),
        "end_time": _clean_field(data.get("end_time"), _TIME_RE),
        "location": _clean_field(data.get("location")),
    }


_CHAT_SYSTEM = """You are athlete15, Veer's personal assistant on Telegram. Veer is an ECE student and Division III volleyball player at NYU. Reply in one to three sentences.

Hard rules:
- Never give training advice. Never tell him to train, rest, push through, or back off.
- Never advise on pain or injuries. You may only restate logged values and their trend when they appear in the context below, and note that judgment calls belong to him and his athletic trainer.
- Only cite numbers that appear in the context, and only for the exact metric
  the context attaches them to. A pain score is not an RPE, a recovery is not
  a sleep number. If a value is not in the context, say you do not have it in
  front of you rather than estimating.
- Plain text only: no markdown, no emoji, no em dashes.

Context:
"""


def chat(text: str, context: str) -> str:
    """Freeform conversational reply grounded in the context block.

    Raises requests.RequestException on transport/HTTP failure or a broken
    Ollama envelope.
    """
    response = requests.post(
        f"{config.OLLAMA_URL}/api/generate",
        json={
            "model": config.OLLAMA_MODEL_FAST,
            "system": _CHAT_SYSTEM + (context or "(none)"),
            "prompt": text,
            "think": False,
            "stream": False,
            "options": {"num_ctx": NUM_CTX, "temperature": CHAT_TEMPERATURE},
        },
        timeout=CHAT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    try:
        raw = response.json().get("response") or ""
    except ValueError as exc:
        raise requests.RequestException("ollama returned non-JSON body") from exc

    return _strip_think(raw).strip()
