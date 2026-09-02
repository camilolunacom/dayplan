"""SQLite storage. One file, WAL mode, so the CLI and the web server can share it."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,   -- "<source>:<external_id>"
    ref           INTEGER UNIQUE,     -- short handle for the CLI (#12)
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT,
    project       TEXT,
    status        TEXT,
    priority      INTEGER DEFAULT 0,  -- normalized 0 none .. 3 high
    due           TEXT,               -- YYYY-MM-DD
    tags          TEXT DEFAULT '[]',  -- JSON array
    notes         TEXT,
    raw           TEXT,               -- provider payload, JSON
    first_seen    TEXT NOT NULL,
    last_synced   TEXT NOT NULL,
    closed        INTEGER NOT NULL DEFAULT 0,
    closed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source);
CREATE INDEX IF NOT EXISTS idx_tasks_closed ON tasks(closed);

CREATE TABLE IF NOT EXISTS plan (
    task_id      TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    day          TEXT NOT NULL,       -- YYYY-MM-DD
    position     INTEGER NOT NULL,
    note         TEXT,
    est_minutes  INTEGER,
    done_local   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plan_day ON plan(day, position);

-- One row per source per sync attempt, so the dashboard can show what each
-- integration is actually doing instead of just how many tasks it has.
CREATE TABLE IF NOT EXISTS sync_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    fetched      INTEGER NOT NULL DEFAULT 0,   -- tasks the provider returned
    added        INTEGER NOT NULL DEFAULT 0,
    updated      INTEGER NOT NULL DEFAULT 0,
    closed       INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    trigger      TEXT                          -- 'manual' | 'scheduled' | 'cli'
);

CREATE INDEX IF NOT EXISTS idx_sync_log_source ON sync_log(source, finished_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
