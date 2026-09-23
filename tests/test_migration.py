"""The new columns must be an additive migration: an existing production
database (created before absence_streak/needs_retriage/reappeared_at
existed) has to pick them up with safe defaults, keeping every row it had."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dayplan import store
from dayplan.db import connect

from _support import make_cfg, remote

OLD_SCHEMA = """
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
    position     INTEGER,
    updated_at   TEXT NOT NULL
);
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


class MigrationIsAdditiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dayplan-migration-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db_path = self.tmp / "old.sqlite"

        raw = sqlite3.connect(self.db_path)
        try:
            raw.executescript(OLD_SCHEMA)
            raw.execute(
                "INSERT INTO tasks(id, ref, source, external_id, title, first_seen, "
                "last_synced, closed) VALUES('ticktick:X', 1, 'ticktick', 'X', 'Old task', "
                "'2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00', 0)"
            )
            raw.execute(
                "INSERT INTO plan(task_id, position, updated_at) VALUES"
                "('ticktick:X', 0, '2025-01-01T00:00:00+00:00')"
            )
            raw.commit()
        finally:
            raw.close()

    def test_connect_adds_new_columns_without_losing_data(self):
        conn = connect(self.db_path)
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            for column in ("absence_streak", "needs_retriage", "reappeared_at"):
                self.assertIn(column, columns)

            row = conn.execute(
                "SELECT title, closed, absence_streak, needs_retriage, reappeared_at "
                "FROM tasks WHERE id = ?",
                ("ticktick:X",),
            ).fetchone()
            self.assertEqual(row["title"], "Old task", "existing data must survive")
            self.assertEqual(row["closed"], 0)
            self.assertEqual(row["absence_streak"], 0, "new column gets a safe default")
            self.assertEqual(row["needs_retriage"], 0)
            self.assertIsNone(row["reappeared_at"])

            plan_row = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:X",)
            ).fetchone()
            self.assertEqual(plan_row["position"], 0, "the existing plan row must survive")
        finally:
            conn.close()

    def test_connect_is_idempotent_on_an_already_migrated_database(self):
        connect(self.db_path).close()
        conn = connect(self.db_path)  # a second connect must not error or duplicate columns
        try:
            columns = [row["name"] for row in conn.execute("PRAGMA table_info(tasks)")]
            self.assertEqual(columns.count("absence_streak"), 1)
        finally:
            conn.close()

    def test_connect_preserves_all_seeded_rows_across_every_table(self):
        raw = sqlite3.connect(self.db_path)
        try:
            raw.execute(
                "INSERT INTO sync_log(source, started_at, finished_at, ok, fetched, "
                "added, updated, closed, error, trigger) VALUES('ticktick', "
                "'2025-01-01T00:00:00+00:00', '2025-01-01T00:00:01+00:00', 1, 1, 1, 0, 0, "
                "NULL, 'cli')"
            )
            raw.execute("INSERT INTO meta(key, value) VALUES('order_revision', '3')")
            raw.commit()
        finally:
            raw.close()

        connect(self.db_path).close()
        connect(self.db_path).close()  # idempotence: a second connect must not disturb data

        conn = connect(self.db_path)
        try:
            task = conn.execute(
                "SELECT title, closed FROM tasks WHERE id = ?", ("ticktick:X",)
            ).fetchone()
            self.assertEqual(task["title"], "Old task")
            self.assertEqual(task["closed"], 0)

            plan_row = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:X",)
            ).fetchone()
            self.assertEqual(plan_row["position"], 0)

            log_row = conn.execute(
                "SELECT source, ok, trigger FROM sync_log WHERE source = 'ticktick'"
            ).fetchone()
            self.assertEqual(log_row["ok"], 1)
            self.assertEqual(log_row["trigger"], "cli")

            meta_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'order_revision'"
            ).fetchone()
            self.assertEqual(meta_row["value"], "3")
        finally:
            conn.close()


class LegacyClosedTaskBackfillTests(unittest.TestCase):
    """A closed task from before `needs_retriage` existed represents a
    provider disappearance under the prior runtime -- there was no other way
    to get to `closed = 1`. Its return must be recognized as a reopen, not
    treated as an ordinary update that leaves its stale plan position in
    place."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dayplan-migration-legacy-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db_path = self.tmp / "old.sqlite"
        self.cfg = make_cfg(self.db_path)

        raw = sqlite3.connect(self.db_path)
        try:
            raw.executescript(OLD_SCHEMA)
            raw.execute(
                "INSERT INTO tasks(id, ref, source, external_id, title, first_seen, "
                "last_synced, closed, closed_at) VALUES('ticktick:X', 1, 'ticktick', 'X', "
                "'Old closed task', '2025-01-01T00:00:00+00:00', "
                "'2025-01-02T00:00:00+00:00', 1, '2025-01-02T00:00:00+00:00')"
            )
            raw.execute(
                "INSERT INTO plan(task_id, position, updated_at) VALUES"
                "('ticktick:X', 0, '2025-01-01T00:00:00+00:00')"
            )
            raw.commit()
        finally:
            raw.close()

    def test_legacy_closed_task_is_flagged_for_retriage_by_the_migration(self):
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT closed, needs_retriage FROM tasks WHERE id = ?", ("ticktick:X",)
            ).fetchone()
            self.assertEqual(row["closed"], 1, "migration must not resurrect the task")
            self.assertEqual(
                row["needs_retriage"], 1,
                "a legacy closed task must be flagged so its return is recognized "
                "as a reopen instead of an ordinary update",
            )

            plan_row = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:X",)
            ).fetchone()
            self.assertIsNotNone(plan_row, "the migration must not delete plan rows")
            self.assertEqual(plan_row["position"], 0)
        finally:
            conn.close()

    def test_legacy_closed_task_returning_after_migration_lands_in_new(self):
        connect(self.db_path).close()  # migrate, flagging the legacy closed row

        with patch(
            "dayplan.store.fetch_all",
            return_value=({"ticktick": [remote("X")]}, {}),
        ):
            store.sync(self.cfg, ["ticktick"], trigger="cli")

        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT closed, needs_retriage, reappeared_at FROM tasks WHERE id = ?",
                ("ticktick:X",),
            ).fetchone()
            self.assertEqual(row["closed"], 0)
            self.assertEqual(row["needs_retriage"], 0, "the transition state must be cleared")
            self.assertIsNotNone(
                row["reappeared_at"], "a recognized reopen must be timestamped as one"
            )

            plan_row = conn.execute(
                "SELECT 1 FROM plan WHERE task_id = ?", ("ticktick:X",)
            ).fetchone()
            self.assertIsNone(plan_row, "the old plan row must be dropped, not reused")

            fresh_ids = [t["id"] for t in store.new_tasks(conn)]
            self.assertIn("ticktick:X", fresh_ids, "it must land in New, not back in its old spot")
        finally:
            conn.close()


class ConcurrentFirstConnectionMigrationTests(unittest.TestCase):
    """Two first callers against the same unmigrated legacy database can each
    see the missing columns before either one adds them, and both attempt the
    same `ALTER TABLE` -- one of them getting a duplicate-column error instead
    of the clean no-op a second `connect()` is supposed to be."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dayplan-migration-race-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db_path = self.tmp / "old.sqlite"

        raw = sqlite3.connect(self.db_path)
        try:
            raw.executescript(OLD_SCHEMA)
            raw.execute(
                "INSERT INTO tasks(id, ref, source, external_id, title, first_seen, "
                "last_synced, closed) VALUES('ticktick:X', 1, 'ticktick', 'X', 'Old task', "
                "'2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00', 1)"
            )
            raw.execute(
                "INSERT INTO plan(task_id, position, updated_at) VALUES"
                "('ticktick:X', 0, '2025-01-01T00:00:00+00:00')"
            )
            raw.commit()
        finally:
            raw.close()

    def test_every_concurrent_first_connect_succeeds_with_correct_columns_and_data(self):
        concurrency = 8
        barrier = threading.Barrier(concurrency)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def connect_lined_up():
            try:
                barrier.wait(timeout=5)  # every thread calls connect() at once
                connect(self.db_path).close()
            except BaseException as exc:  # noqa: BLE001 - captured for the assertion
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=connect_lined_up) for _ in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "a racing connect() must not deadlock")

        self.assertEqual(errors, [], "every concurrent first connect() must succeed")

        conn = connect(self.db_path)
        try:
            columns = [row["name"] for row in conn.execute("PRAGMA table_info(tasks)")]
            for column in ("absence_streak", "needs_retriage", "reappeared_at"):
                self.assertEqual(columns.count(column), 1, f"{column} must exist exactly once")

            row = conn.execute(
                "SELECT title, closed, needs_retriage FROM tasks WHERE id = ?", ("ticktick:X",)
            ).fetchone()
            self.assertEqual(row["title"], "Old task")
            self.assertEqual(row["closed"], 1)
            self.assertEqual(row["needs_retriage"], 1, "the legacy backfill must still apply")

            plan_row = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:X",)
            ).fetchone()
            self.assertEqual(plan_row["position"], 0, "the existing plan row must survive")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
