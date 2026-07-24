"""SQLite schema and helpers. Phase 3.

Design notes worth understanding before you extend this:

- Raw API payloads are stored as JSON text next to parsed columns. When you later
  want a field you did not originally parse, the history is still there. Storage is
  free at this scale, lost data is not.
- Dates are stored as ISO-8601 strings ('2026-07-22'). SQLite has no date type.
  Sorting and range queries work correctly on ISO strings because the format is
  lexicographically ordered.
- Timestamps are UTC. Convert to America/New_York only when displaying.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src import config

SCHEMA = """
-- ---------- health ----------

CREATE TABLE IF NOT EXISTS daily_metrics (
    date              TEXT PRIMARY KEY,   -- 'YYYY-MM-DD', local date of wake
    recovery_score    INTEGER,            -- 0-100
    hrv_ms            REAL,
    resting_hr        INTEGER,
    sleep_hours       REAL,
    sleep_performance INTEGER,            -- 0-100
    strain            REAL,               -- WHOOP day strain 0-21
    raw_json          TEXT,
    fetched_at        TEXT NOT NULL
);

-- WHOOP's own recorded activities. Deliberately NOT the sessions table below:
-- these are measured by the strap and never hand entered, and the lift WHOOP saw
-- as 48 minutes of elevated heart rate is a different measurement from the same
-- lift you logged as "60 min rpe 8". Keeping both lets you compare them; merging
-- them would silently destroy that.
CREATE TABLE IF NOT EXISTS whoop_workouts (
    id           TEXT PRIMARY KEY,       -- WHOOP v2 workout UUID
    date         TEXT NOT NULL,          -- local date the workout started
    sport_id     INTEGER,                -- WHOOP's integer, not a name
    start_utc    TEXT NOT NULL,
    end_utc      TEXT,
    duration_min INTEGER,
    strain       REAL,
    average_hr   INTEGER,
    max_hr       INTEGER,
    kilojoule    REAL,
    raw_json     TEXT,
    fetched_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_whoop_workouts_date ON whoop_workouts(date);

-- ---------- training ----------

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY,
    date         TEXT NOT NULL,           -- 'YYYY-MM-DD'
    type         TEXT NOT NULL,           -- lift | court | conditioning | mobility
    block        TEXT,                    -- offseason | preseason | in_season
    location     TEXT,                    -- facility | nyu | other
    duration_min INTEGER,
    rpe          INTEGER,                 -- session RPE, 1-10
    notes        TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);

CREATE TABLE IF NOT EXISTS lifts (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    exercise   TEXT NOT NULL,
    sets       INTEGER,
    reps       INTEGER,
    weight_lb  REAL,
    notes      TEXT
);

CREATE INDEX IF NOT EXISTS idx_lifts_exercise ON lifts(exercise);

-- ---------- injuries ----------

CREATE TABLE IF NOT EXISTS injuries (
    id            INTEGER PRIMARY KEY,
    body_part     TEXT NOT NULL,          -- shoulder | knee | ankle | back | ...
    side          TEXT,                   -- left | right | bilateral | na
    description   TEXT,
    onset_date    TEXT NOT NULL,
    status        TEXT NOT NULL,          -- active | managing | resolved
    resolved_date TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS injury_log (
    id         INTEGER PRIMARY KEY,
    injury_id  INTEGER NOT NULL REFERENCES injuries(id) ON DELETE CASCADE,
    date       TEXT NOT NULL,
    pain_0_10  INTEGER,
    limited    INTEGER,                   -- 0 or 1, did it limit training
    notes      TEXT
);

CREATE INDEX IF NOT EXISTS idx_injury_log_date ON injury_log(date);

-- ---------- academics and schedule ----------

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,         -- Notion page id
    title       TEXT NOT NULL,
    course      TEXT,
    due_date    TEXT,
    status      TEXT,
    raw_json    TEXT,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    uid        TEXT PRIMARY KEY,          -- iCal UID
    summary    TEXT NOT NULL,
    location   TEXT,
    start_utc  TEXT NOT NULL,
    end_utc    TEXT,
    source     TEXT,                      -- athletics | google | ...
    raw_json   TEXT,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_utc);

-- ---------- inbound log ----------

-- Every message sent to the bot, stored verbatim before anything tries to
-- interpret it. Parsing is lossy and the parser will be replaced (simple regex
-- now, possibly a local model later), so raw_text is the record of truth and the
-- parsed columns are a derived cache that can be recomputed from it.
CREATE TABLE IF NOT EXISTS log_entries (
    id           INTEGER PRIMARY KEY,
    received_at  TEXT NOT NULL,          -- UTC ISO-8601
    raw_text     TEXT NOT NULL,          -- exactly as sent, never edited
    parsed_json  TEXT,                   -- parse_entry() output, NULL until parsed
    parse_status TEXT,                   -- parsed | partial | unparsed | error
    parse_method TEXT                    -- simple | llm
);

CREATE INDEX IF NOT EXISTS idx_log_entries_received ON log_entries(received_at);

-- ---------- output log ----------

CREATE TABLE IF NOT EXISTS briefs (
    date    TEXT PRIMARY KEY,
    text    TEXT NOT NULL,
    sent_at TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with sensible defaults.

    row_factory makes rows behave like dicts (row["recovery_score"]) instead of
    tuples (row[1]). Foreign keys are off by default in SQLite, which means the
    ON DELETE CASCADE above silently does nothing unless you enable them.
    """
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | None = None) -> None:
    """Create all tables if they do not exist. Safe to call on every startup."""
    with connect(path) as conn:
        conn.executescript(SCHEMA)


if __name__ == "__main__":
    init_db()
    print(f"initialized {config.DB_PATH}")


def backup(dest_dir: Path | None = None, path: Path | None = None) -> Path | None:
    """Copy the database to the archive drive. Phase 3.

    Uses sqlite3's own backup API rather than shutil.copy. Copying the file while a
    write is in progress can produce a corrupt snapshot; the backup API takes a
    consistent one even with the connection open.

    Snapshots are dated (assistant-YYYY-MM-DD.db) instead of overwriting one file.
    A single rolling copy would let a corrupted source database overwrite the last
    good backup on the next run, which defeats the point of having one. Nothing
    prunes old snapshots yet.

    Args:
        dest_dir: destination directory, defaulting to config.BACKUP_DIR.
        path: source database, defaulting to config.DB_PATH. Same override the
            rest of this module takes, so tests can back up a temp database.

    Returns the path written, or None if BACKUP_DIR is unset.

    Raises:
        OSError: if the destination is unreachable, e.g. the archive drive is not
            mounted. The caller decides whether that kills the run.
    """
    dest_dir = dest_dir or config.BACKUP_DIR
    if dest_dir is None:
        # Skip, do not raise: an unset BACKUP_DIR is a valid configuration. It is
        # still logged, because a backup that silently never happens is exactly
        # the failure you find out about when the drive is already dead.
        logging.info("BACKUP_DIR not set, skipping backup")
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_path = dest_dir / f"assistant-{today}.db"

    source = connect(path)
    destination = sqlite3.connect(dest_path)
    try:
        # `with conn:` is a transaction context manager in sqlite3, not a closing
        # one. It commits or rolls back and leaves the connection open, so the
        # closes below are done by hand.
        with destination:
            source.backup(destination)
    finally:
        destination.close()
        source.close()

    logging.info("database backed up to %s", dest_path)
    return dest_path


# Columns of daily_metrics that upsert_daily_metrics accepts, excluding the date
# primary key and the bookkeeping columns.
_METRIC_COLUMNS = (
    "recovery_score",
    "hrv_ms",
    "resting_hr",
    "sleep_hours",
    "sleep_performance",
    "strain",
)

_UPSERT_DAILY_METRICS = f"""
INSERT INTO daily_metrics (date, {", ".join(_METRIC_COLUMNS)}, raw_json, fetched_at)
VALUES (:date, {", ".join(f":{c}" for c in _METRIC_COLUMNS)}, :raw_json, :fetched_at)
ON CONFLICT(date) DO UPDATE SET
    {", ".join(f"{c} = COALESCE(excluded.{c}, daily_metrics.{c})"
               for c in (*_METRIC_COLUMNS, "raw_json"))},
    fetched_at = excluded.fetched_at
"""


def upsert_daily_metrics(row: dict, path: Path | None = None) -> None:
    """Insert or update one day's health metrics, keyed on date.

    Partial updates are the point. WHOOP recovery and sleep come from different
    endpoints and can land in separate calls, so a column absent from `row` keeps
    whatever is already stored rather than nulling it (the COALESCE above). The
    consequence: this helper cannot erase a value, only set or replace one. That
    is the right tradeoff for health history you only ever accumulate.

    Args:
        row: must contain 'date' as 'YYYY-MM-DD'. May contain any of
            recovery_score, hrv_ms, resting_hr, sleep_hours, sleep_performance,
            strain. 'raw' is JSON-encoded into raw_json; 'fetched_at' defaults to
            now in UTC.

    Raises:
        ValueError: if 'date' is missing or a key is not a real column. Unknown
            keys are rejected rather than ignored so a typo does not look like a
            successful write that stored nothing.
    """
    if not row.get("date"):
        raise ValueError("row needs a 'date' key ('YYYY-MM-DD')")

    allowed = {"date", "raw", "raw_json", "fetched_at", *_METRIC_COLUMNS}
    unknown = set(row) - allowed
    if unknown:
        raise ValueError(f"unknown daily_metrics fields: {sorted(unknown)}")

    raw_json = row.get("raw_json")
    if raw_json is None and row.get("raw") is not None:
        raw_json = json.dumps(row["raw"], default=str)

    params = {
        "date": row["date"],
        "raw_json": raw_json,
        "fetched_at": row.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
        **{column: row.get(column) for column in _METRIC_COLUMNS},
    }

    conn = connect(path)
    try:
        with conn:
            conn.execute(_UPSERT_DAILY_METRICS, params)
    finally:
        conn.close()


_WORKOUT_COLUMNS = (
    "date",
    "sport_id",
    "start_utc",
    "end_utc",
    "duration_min",
    "strain",
    "average_hr",
    "max_hr",
    "kilojoule",
)

_UPSERT_WORKOUT = f"""
INSERT INTO whoop_workouts (id, {", ".join(_WORKOUT_COLUMNS)}, raw_json, fetched_at)
VALUES (:id, {", ".join(f":{c}" for c in _WORKOUT_COLUMNS)}, :raw_json, :fetched_at)
ON CONFLICT(id) DO UPDATE SET
    {", ".join(f"{c} = excluded.{c}" for c in (*_WORKOUT_COLUMNS, "raw_json"))},
    fetched_at = excluded.fetched_at
"""


def upsert_whoop_workout(row: dict, path: Path | None = None) -> None:
    """Insert or update one WHOOP workout, keyed on WHOOP's UUID.

    Overwrites rather than COALESCEs, unlike upsert_daily_metrics. A workout is
    re-fetched constantly because the collection endpoint defaults to a trailing
    seven day window, and WHOOP revises a workout's strain after the fact once
    late heart rate samples sync. The newest version from WHOOP is always the one
    to keep.

    Raises:
        ValueError: if 'id' is missing.
    """
    if not row.get("id"):
        raise ValueError("workout row needs an 'id'")

    raw_json = row.get("raw_json")
    if raw_json is None and row.get("raw") is not None:
        raw_json = json.dumps(row["raw"], default=str)

    params = {
        "id": row["id"],
        "raw_json": raw_json,
        "fetched_at": row.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
        **{column: row.get(column) for column in _WORKOUT_COLUMNS},
    }

    conn = connect(path)
    try:
        with conn:
            conn.execute(_UPSERT_WORKOUT, params)
    finally:
        conn.close()


_TASK_COLUMNS = ("title", "course", "due_date", "status")

_UPSERT_TASK = f"""
INSERT INTO tasks (id, {", ".join(_TASK_COLUMNS)}, raw_json, fetched_at)
VALUES (:id, {", ".join(f":{c}" for c in _TASK_COLUMNS)}, :raw_json, :fetched_at)
ON CONFLICT(id) DO UPDATE SET
    {", ".join(f"{c} = excluded.{c}" for c in (*_TASK_COLUMNS, "raw_json"))},
    fetched_at = excluded.fetched_at
"""


def upsert_task(row: dict, path: Path | None = None) -> None:
    """Insert or update one Notion task, keyed on the Notion page id.

    Overwrites rather than COALESCEs, like whoop_workouts and unlike
    daily_metrics: Notion is the source of truth for these rows and every edit
    there, including clearing a due date back to NULL, must propagate here on the
    next fetch. COALESCE semantics would pin the old date forever.

    One asymmetry to know about: fetch() filters out Done tasks, so a task
    finished in Notion stops being re-fetched and its row here keeps the last
    status seen before it was done. The table is a cache of what the briefing
    showed, not a mirror of the whole database.

    Args:
        row: needs 'id' (Notion page id). Extra keys like 'type' and 'url' ride
            along inside raw_json rather than getting columns, since the phase 3
            schema predates them and history is recoverable from raw anyway.

    Raises:
        ValueError: if 'id' is missing.
    """
    if not row.get("id"):
        raise ValueError("task row needs an 'id' (Notion page id)")

    raw_json = row.get("raw_json")
    if raw_json is None and row.get("raw") is not None:
        raw_json = json.dumps(row["raw"], default=str)

    params = {
        "id": row["id"],
        "raw_json": raw_json,
        "fetched_at": row.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
        **{column: row.get(column) for column in _TASK_COLUMNS},
    }

    conn = connect(path)
    try:
        with conn:
            conn.execute(_UPSERT_TASK, params)
    finally:
        conn.close()


def insert_log_entry(
    raw_text: str,
    received_at: str | None = None,
    path: Path | None = None,
) -> int:
    """Store an inbound message verbatim and return its id.

    Called before the parser runs, on purpose. The parser is the part most likely
    to be wrong or to change, and a session typed out on a phone at the end of a
    lift exists nowhere else. Getting the text onto disk first means the worst a
    parser bug can do is leave parsed_json NULL, never lose the entry.
    """
    conn = connect(path)
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO log_entries (received_at, raw_text) VALUES (?, ?)",
                (received_at or datetime.now(timezone.utc).isoformat(), raw_text),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def record_parse(
    entry_id: int,
    parsed: dict | None,
    parse_status: str,
    parse_method: str,
    path: Path | None = None,
) -> None:
    """Attach a parse result to an already stored log entry.

    Separate from insert_log_entry so re-parsing history later (with a better
    parser) is an UPDATE over existing rows rather than a migration.
    """
    conn = connect(path)
    try:
        with conn:
            conn.execute(
                """
                UPDATE log_entries
                   SET parsed_json = ?, parse_status = ?, parse_method = ?
                 WHERE id = ?
                """,
                (
                    json.dumps(parsed, default=str) if parsed is not None else None,
                    parse_status,
                    parse_method,
                    entry_id,
                ),
            )
    finally:
        conn.close()


def record_brief(
    date: str,
    text: str,
    sent_at: str | None = None,
    path: Path | None = None,
) -> None:
    """Store the assembled briefing text for a given local date.

    date is the local date the briefing is *for*, so re-running the job the same
    morning replaces that day's row instead of adding a second one. sent_at is
    left NULL when delivery failed, which is what distinguishes "we built a
    briefing and Telegram rejected it" from "we sent it" after the fact.
    """
    conn = connect(path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO briefs (date, text, sent_at) VALUES (?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    text = excluded.text,
                    sent_at = excluded.sent_at
                """,
                (date, text, sent_at),
            )
    finally:
        conn.close()
