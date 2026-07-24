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
