"""Real multi-process regression test for the migration lock.

`connect()` runs `_add_missing_columns` (its own `BEGIN IMMEDIATE`) followed
by several legacy migration helpers (`_migrate_plan_position_nullable`,
`_migrate_days_into_one_list`, `_drop_unused_plan_columns`, `_drop_plan_day`)
that each open and commit their own transaction but are not otherwise
serialized against a second process racing the very same first-ever
`connect()` on an old, unmigrated database. Two such callers can interleave
mid-chain and corrupt the migration (`no such column: day`,
`database is locked`, or a partially-migrated shape).

This test seeds the oldest supported legacy `plan` schema (`day` column,
`position INTEGER NOT NULL`, and the retired `est_minutes`/`done_local`/
`note` columns) plus a closed task, starts several separate OS processes
behind a shared start barrier, and has every one of them call `connect()` on
the same database file. A second barrier is injected -- by monkeypatching
`dayplan.db._migrate_days_into_one_list` in the parent before forking, so
every forked child inherits the patched module -- to force all processes
into the legacy-migration critical section at the same instant instead of
relying on incidental OS scheduling. That makes the race deterministic: with
a lock covering the whole schema/migration sequence, only one process can
ever be inside that critical section at a time, so the injected rendezvous
barrier always times out (proving serialization); without the lock, every
process reaches it together and the legacy helpers race for real.
"""

from __future__ import annotations

import multiprocessing
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dayplan import db as db_module
from dayplan.db import connect

OLDEST_SCHEMA = """
CREATE TABLE tasks (
    id            TEXT PRIMARY KEY,
    ref           INTEGER UNIQUE,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT,
    project       TEXT,
    status        TEXT,
    priority      INTEGER DEFAULT 0,
    due           TEXT,
    tags          TEXT DEFAULT '[]',
    notes         TEXT,
    raw           TEXT,
    first_seen    TEXT NOT NULL,
    last_synced   TEXT NOT NULL,
    closed        INTEGER NOT NULL DEFAULT 0,
    closed_at     TEXT
);
CREATE TABLE plan (
    task_id      TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    day          TEXT NOT NULL,
    position     INTEGER NOT NULL,
    updated_at   TEXT NOT NULL,
    est_minutes  INTEGER,
    done_local   INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);
CREATE INDEX idx_plan_day ON plan(day, position);
CREATE TABLE sync_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    fetched      INTEGER NOT NULL DEFAULT 0,
    added        INTEGER NOT NULL DEFAULT 0,
    updated      INTEGER NOT NULL DEFAULT 0,
    closed       INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    trigger      TEXT
);
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

PROCESS_COUNT = 5


def _seed(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(OLDEST_SCHEMA)
        raw.execute(
            "INSERT INTO tasks(id, ref, source, external_id, title, first_seen, "
            "last_synced, closed) VALUES('ticktick:X', 1, 'ticktick', 'X', 'Listed task', "
            "'2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00', 0)"
        )
        raw.execute(
            "INSERT INTO plan(task_id, day, position, updated_at) VALUES"
            "('ticktick:X', 'list', 0, '2025-01-01T00:00:00+00:00')"
        )
        raw.execute(
            "INSERT INTO tasks(id, ref, source, external_id, title, first_seen, "
            "last_synced, closed) VALUES('ticktick:Y', 2, 'ticktick', 'Y', 'Dated-bucket task', "
            "'2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00', 0)"
        )
        raw.execute(
            "INSERT INTO plan(task_id, day, position, updated_at) VALUES"
            "('ticktick:Y', '2025-01-01', 0, '2025-01-01T00:00:00+00:00')"
        )
        raw.execute(
            "INSERT INTO tasks(id, ref, source, external_id, title, first_seen, "
            "last_synced, closed, closed_at) VALUES('ticktick:Z', 3, 'ticktick', 'Z', "
            "'Closed before retriage existed', '2025-01-01T00:00:00+00:00', "
            "'2025-01-02T00:00:00+00:00', 1, '2025-01-02T00:00:00+00:00')"
        )
        raw.execute(
            "INSERT INTO sync_log(source, started_at, finished_at, ok, fetched, added, "
            "updated, closed, error, trigger) VALUES('ticktick', "
            "'2025-01-01T00:00:00+00:00', '2025-01-01T00:00:01+00:00', 1, 3, 3, 0, 0, "
            "NULL, 'cli')"
        )
        raw.execute("INSERT INTO meta(key, value) VALUES('order_revision', '5')")
        raw.commit()
    finally:
        raw.close()


def _worker(db_path_str: str, start_barrier, queue) -> None:
    try:
        start_barrier.wait(timeout=15)
        conn = connect(Path(db_path_str))
        conn.close()
        queue.put((os.getpid(), "ok", None))
    except BaseException as exc:  # noqa: BLE001 - reported to the parent, not raised here
        queue.put((os.getpid(), "error", f"{type(exc).__name__}: {exc}"))


class MigrationLockMultiprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dayplan-migration-mp-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db_path = self.tmp / "old.sqlite"
        _seed(self.db_path)

    def test_every_process_completes_the_full_migration_chain_exactly_once(self):
        ctx = multiprocessing.get_context("fork")
        start_barrier = ctx.Barrier(PROCESS_COUNT)
        race_barrier = ctx.Barrier(PROCESS_COUNT)
        queue = ctx.Queue()

        original_migrate_days = db_module._migrate_days_into_one_list

        def rendezvous_then_migrate(conn):
            # Forces every process into this legacy-migration critical
            # section at the same instant, instead of leaving the race to
            # incidental OS scheduling. A lock covering the whole chain
            # means only one process is ever here at a time, so this always
            # times out (proving serialization) once the fix is in place.
            try:
                race_barrier.wait(timeout=2)
            except Exception:  # noqa: BLE001 - broken/timed-out barrier is the expected, safe case
                pass
            return original_migrate_days(conn)

        with patch.object(
            db_module, "_migrate_days_into_one_list", side_effect=rendezvous_then_migrate
        ):
            processes = [
                ctx.Process(target=_worker, args=(str(self.db_path), start_barrier, queue))
                for _ in range(PROCESS_COUNT)
            ]
            for p in processes:
                p.start()

            results = []
            for _ in range(PROCESS_COUNT):
                results.append(queue.get(timeout=30))

            for p in processes:
                p.join(timeout=10)
                self.assertFalse(p.is_alive(), "a racing connect() must not hang a process")

        errors = [r for r in results if r[1] != "ok"]
        self.assertEqual(
            errors, [], f"every concurrent first connect() must succeed cleanly, got: {errors}"
        )

        conn = connect(self.db_path)
        try:
            columns = [row["name"] for row in conn.execute("PRAGMA table_info(tasks)")]
            for column in ("absence_streak", "needs_retriage", "reappeared_at"):
                self.assertEqual(columns.count(column), 1, f"{column} must exist exactly once")

            plan_columns = {row["name"] for row in conn.execute("PRAGMA table_info(plan)")}
            self.assertNotIn("day", plan_columns, "the legacy day column must be dropped")
            for column in ("est_minutes", "done_local", "note"):
                self.assertNotIn(column, plan_columns, f"{column} must be dropped")

            task_x = conn.execute(
                "SELECT closed, needs_retriage FROM tasks WHERE id = ?", ("ticktick:X",)
            ).fetchone()
            self.assertEqual(task_x["closed"], 0)
            self.assertEqual(task_x["needs_retriage"], 0)

            task_z = conn.execute(
                "SELECT closed, needs_retriage FROM tasks WHERE id = ?", ("ticktick:Z",)
            ).fetchone()
            self.assertEqual(task_z["closed"], 1, "migration must not resurrect the closed task")
            self.assertEqual(
                task_z["needs_retriage"], 1,
                "the pre-retriage closed task must be flagged so its return is a reopen",
            )

            plan_x = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:X",)
            ).fetchone()
            plan_y = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:Y",)
            ).fetchone()
            self.assertIsNotNone(plan_x, "X's plan row must survive the migration")
            self.assertIsNotNone(plan_y, "Y's plan row must survive the migration")
            self.assertEqual(
                {plan_x["position"], plan_y["position"]},
                {0, 1},
                "the collapsed list must be renumbered 0..n-1 with no gaps or dupes",
            )

            sync_row = conn.execute(
                "SELECT source, ok, trigger FROM sync_log WHERE source = 'ticktick'"
            ).fetchone()
            self.assertEqual(sync_row["ok"], 1)
            self.assertEqual(sync_row["trigger"], "cli")

            meta_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'order_revision'"
            ).fetchone()
            self.assertEqual(meta_row["value"], "5", "meta must survive untouched")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
