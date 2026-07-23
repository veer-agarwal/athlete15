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

import sqlite3
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


def backup(dest_dir: Path | None = None) -> Path | None:
    """Copy the database to the archive drive. Phase 3.

    Use sqlite3's own backup API rather than shutil.copy. Copying the file while a
    write is in progress can produce a corrupt snapshot; the backup API takes a
    consistent one even with the connection open.

        with connect() as src, sqlite3.connect(dest) as dst:
            src.backup(dst)

    Returns the path written, or None if BACKUP_DIR is unset.
    """
    raise NotImplementedError("phase 3")
