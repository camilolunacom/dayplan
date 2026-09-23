"""SQLite storage. One file, WAL mode, so the CLI and the web server can share it."""

from __future__ import annotations

import contextlib
import fcntl
import os
import sqlite3
from pathlib import Path
from typing import Iterator

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
    closed_at     TEXT,
    toggl_project    TEXT,        -- the NAME; the only thing Toggl acts on
    toggl_project_id INTEGER      -- reference only, the extension ignores it
);

CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source);
CREATE INDEX IF NOT EXISTS idx_tasks_closed ON tasks(closed);

-- What is his rather than the providers': the order. The row existing means
-- "in the list"; position NULL means "seen, not ranked". No row at all means
-- the task is still in the new pile.
CREATE TABLE IF NOT EXISTS plan (
    task_id      TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    position     INTEGER,             -- NULL = default order
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plan_position ON plan(position);

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


def _migrate_plan_position_nullable(conn: sqlite3.Connection) -> None:
    """Older databases declared plan.position NOT NULL.

    SQLite cannot relax a column constraint in place, so rebuild the table
    when the old definition is still there. Data is preserved.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'plan'"
    ).fetchone()
    if not row or "position     INTEGER NOT NULL" not in (row["sql"] or ""):
        return
    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        BEGIN;
        CREATE TABLE plan_new (
            task_id      TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            day          TEXT NOT NULL,
            position     INTEGER,
            updated_at   TEXT NOT NULL
        );
        INSERT INTO plan_new SELECT task_id, day, position, updated_at FROM plan;
        DROP TABLE plan;
        ALTER TABLE plan_new RENAME TO plan;
        COMMIT;
        PRAGMA foreign_keys=ON;
        """
    )
    conn.commit()


def _migrate_days_into_one_list(conn: sqlite3.Connection) -> None:
    """Collapse the old per-day buckets into the single list.

    Rows written before the list model carry a date in `day` and a position
    scoped to that date. Left alone they all read as hand-ordered, in an order
    that means nothing. Move them onto the list, keeping their relative
    sequence, and renumber from zero.
    """
    if "day" not in {row["name"] for row in conn.execute("PRAGMA table_info(plan)")}:
        return  # already collapsed, or a database created after `day` was dropped
    stale = conn.execute("SELECT COUNT(*) AS n FROM plan WHERE day != 'list'").fetchone()["n"]
    if not stale:
        return
    # Read the merged order before touching `day`: rows already on the list keep
    # their sequence and the dated ones follow, oldest day first. Renumber the
    # whole set, not just the migrated part, or the two ranges collide on 0..n.
    rows = conn.execute(
        "SELECT task_id FROM plan WHERE position IS NOT NULL "
        "ORDER BY CASE WHEN day = 'list' THEN 0 ELSE 1 END, day, position"
    ).fetchall()
    conn.execute("UPDATE plan SET day = 'list' WHERE day != 'list'")
    for index, row in enumerate(rows):
        conn.execute(
            "UPDATE plan SET position = ? WHERE task_id = ?", (index, row["task_id"])
        )
    conn.commit()


# Fields that were invented rather than asked for, and removed once that was
# clear: an estimate nobody fills in is noise, a local done tick never reached
# the source so the next sync undid it, and a note is not what this app is for.
DROPPED_PLAN_COLUMNS = ("est_minutes", "done_local", "note")


def _drop_plan_day(conn: sqlite3.Connection) -> None:
    """Retire `day`, which every row now sets to the same literal.

    Runs after _migrate_days_into_one_list has folded the dated buckets in, so
    by here the column carries no information. SQLite refuses to drop an
    indexed column, hence dropping idx_plan_day first.
    """
    if "day" not in {row["name"] for row in conn.execute("PRAGMA table_info(plan)")}:
        return
    conn.execute("DROP INDEX IF EXISTS idx_plan_day")
    conn.execute("ALTER TABLE plan DROP COLUMN day")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plan_position ON plan(position)")
    conn.commit()


def _drop_unused_plan_columns(conn: sqlite3.Connection) -> None:
    have = {row["name"] for row in conn.execute("PRAGMA table_info(plan)")}
    stale = have & set(DROPPED_PLAN_COLUMNS)
    for column in stale:
        conn.execute(f"ALTER TABLE plan DROP COLUMN {column}")
    if stale:
        conn.commit()


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """ALTER TABLE ADD COLUMN is safe and cheap in SQLite; CREATE TABLE IF NOT
    EXISTS will not add a column to a table that already exists.

    `BEGIN IMMEDIATE` before the first `PRAGMA table_info` read, and rechecking
    it once the lock is held, closes a race between two first-ever callers
    (two threads, or the CLI and the server starting at once against the same
    unmigrated file): without it both can see the same missing columns and
    both attempt the same `ALTER TABLE`, and the loser gets a duplicate-column
    error instead of a clean no-op.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        have = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        wanted = {
            "toggl_project_id": "INTEGER",
            "toggl_project": "TEXT",
            # Absence hysteresis: a task only counts as genuinely gone from a
            # source after two consecutive non-empty syncs omit it in a row, so
            # one partial/flaky provider response never closes it. needs_retriage
            # marks a task that was closed this way, so its return -- not just any
            # reopen -- is the trigger that clears its old plan row. reappeared_at
            # is the timestamp that return gets, so it sorts as a fresh New-pile
            # arrival instead of by its original first_seen.
            "absence_streak": "INTEGER NOT NULL DEFAULT 0",
            "needs_retriage": "INTEGER NOT NULL DEFAULT 0",
            "reappeared_at": "TEXT",
        }
        added = False
        retriage_added = False
        for column, kind in wanted.items():
            if column not in have:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {kind}")
                added = True
                if column == "needs_retriage":
                    retriage_added = True

        if retriage_added:
            # Every legacy `closed = 1` row got there under a runtime that had
            # no absence hysteresis and no retriage flag at all: back then
            # `closed` meant exactly what `needs_retriage` means now, a
            # provider disappearance. Flag them the same way a freshly
            # confirmed departure would be, so a later return is recognized
            # as a reopen -- and drops its stale plan position -- instead of
            # being treated as an ordinary update. Deletion still waits for
            # that actual return; the migration itself never touches `plan`.
            conn.execute("UPDATE tasks SET needs_retriage = 1 WHERE closed = 1")

        if added:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise


def _migration_lock_path(db_path: Path) -> Path:
    return db_path.parent / f"{db_path.name}.migrate.lock"


@contextlib.contextmanager
def _migration_lock(db_path: Path) -> Iterator[None]:
    """Serialize schema creation and the whole migration chain across every
    thread and process that shares this database path.

    `_add_missing_columns` guards itself with `BEGIN IMMEDIATE`, but the
    legacy migration helpers that run after it
    (`_migrate_plan_position_nullable`, `_migrate_days_into_one_list`,
    `_drop_unused_plan_columns`, `_drop_plan_day`) each start and commit
    their own transaction and are not otherwise serialized: two first
    `connect()` calls against an older schema can interleave mid-chain and
    hit `no such column: day` or `database is locked`. `flock` on a file next
    to the database serializes the entire sequence regardless of process
    boundary; opening an independent fd per call means the lock is released
    by close (or by the process dying) without needing any cleanup
    handshake, and it coexists fine with the inner `BEGIN IMMEDIATE`.
    """
    lock_path = _migration_lock_path(db_path)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False because FastAPI runs a generator dependency and
    # the endpoint body on separate threadpool threads, so a connection opened
    # in one is used in the other and sqlite3 refuses by default. Safe here:
    # every request opens its own connection and nothing shares one, and the
    # CLI is single-threaded. Without this the API 500s intermittently --
    # sequential calls happen to reuse a thread, parallel ones do not.
    conn = sqlite3.connect(db_path, timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with _migration_lock(db_path):
        # busy_timeout first and foremost: it defaults to 0, so any pragma or
        # DDL below that briefly contends with another connection before this
        # is set would raise "database is locked" immediately instead of
        # retrying -- exactly the failure this lock exists to prevent.
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
        _add_missing_columns(conn)
        _migrate_plan_position_nullable(conn)
        _migrate_days_into_one_list(conn)
        _drop_unused_plan_columns(conn)
        _drop_plan_day(conn)
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
