"""Multi-process regression test proving `_sync_lock` spans the entire
fetch-through-commit reconciliation, not just the provider fetch step.

`test_sync_serialization.py` only proves two `sync()` calls never have their
`fetch_all` calls running at the same time. That is also true of a *narrower*
lock that wraps only the fetch step (or releases right after it) -- which
would still let two real OS processes race the absence-streak bookkeeping
during reconciliation: a later process could read a task's `absence_streak`
before an earlier process's confirmed-departure commit has landed, and both
would write the same next value instead of the correctly incremented one.

This test pauses one process's own reconciliation `commit()` (via a
connection wrapper) after it has already fetched, starts a second process
while the first is paused there, and asserts the second process's own
`fetch_all` is not entered until the first process's commit has actually
happened. It then lets both finish and checks the resulting absence-streak /
closed / plan / revision state matches what strictly serialized execution
would produce.
"""

from __future__ import annotations

import multiprocessing
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dayplan import store
from dayplan.db import connect

from _support import make_cfg, remote


class _PausingConnection:
    """Wraps a real sqlite3 connection; pauses inside commit() until told to
    proceed, so the test can hold "fetch done, reconciliation not yet
    committed" as a real, observable state instead of inferring it from
    timing."""

    def __init__(self, real_conn, committing_event, release_event, committed_event):
        object.__setattr__(self, "_real", real_conn)
        object.__setattr__(self, "_committing_event", committing_event)
        object.__setattr__(self, "_release_event", release_event)
        object.__setattr__(self, "_committed_event", committed_event)

    def commit(self):
        self._committing_event.set()
        self._release_event.wait(timeout=15)
        self._real.commit()
        self._committed_event.set()

    def close(self):
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _worker_p(db_path_str, entered_fetch, committing, release_commit, committed, queue):
    cfg = make_cfg(Path(db_path_str))

    def fake_fetch_all(cfg, sources):
        entered_fetch.set()
        return ({"ticktick": [remote("B")]}, {})

    def wrapped_connect(path):
        real = connect(path)
        return _PausingConnection(real, committing, release_commit, committed)

    try:
        with patch("dayplan.store.fetch_all", side_effect=fake_fetch_all), patch(
            "dayplan.store.connect", side_effect=wrapped_connect
        ):
            store.sync(cfg, ["ticktick"], trigger="cli")
        queue.put(("p", "ok", None))
    except BaseException as exc:  # noqa: BLE001 - reported to the parent, not raised here
        queue.put(("p", "error", f"{type(exc).__name__}: {exc}"))


def _worker_q(db_path_str, entered_fetch, queue):
    cfg = make_cfg(Path(db_path_str))

    def fake_fetch_all(cfg, sources):
        entered_fetch.set()
        return ({"ticktick": [remote("B")]}, {})

    try:
        with patch("dayplan.store.fetch_all", side_effect=fake_fetch_all):
            store.sync(cfg, ["ticktick"], trigger="cli")
        queue.put(("q", "ok", None))
    except BaseException as exc:  # noqa: BLE001 - reported to the parent, not raised here
        queue.put(("q", "error", f"{type(exc).__name__}: {exc}"))


class FetchThroughCommitExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dayplan-sync-lock-mp-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.cfg = make_cfg(self.tmp / "dayplan.sqlite")

        with patch(
            "dayplan.store.fetch_all",
            return_value=({"ticktick": [remote("A"), remote("B")]}, {}),
        ):
            store.sync(self.cfg, ["ticktick"], trigger="cli")
        conn = connect(self.cfg.db_path)
        try:
            store.set_order(conn, ["ticktick:A", "ticktick:B"])
        finally:
            conn.close()

    def test_second_process_cannot_fetch_until_first_process_commits(self):
        ctx = multiprocessing.get_context("fork")
        p_entered_fetch = ctx.Event()
        p_committing = ctx.Event()
        p_release_commit = ctx.Event()
        p_committed = ctx.Event()
        q_entered_fetch = ctx.Event()
        queue = ctx.Queue()

        p = ctx.Process(
            target=_worker_p,
            args=(
                str(self.cfg.db_path),
                p_entered_fetch,
                p_committing,
                p_release_commit,
                p_committed,
                queue,
            ),
        )
        p.start()

        self.assertTrue(p_committing.wait(timeout=10), "process A must reach its paused commit")
        self.assertTrue(p_entered_fetch.is_set(), "A must have fetched before its commit")

        q = ctx.Process(target=_worker_q, args=(str(self.cfg.db_path), q_entered_fetch, queue))
        q.start()

        # A is still paused inside its commit; B must not be able to reach
        # its own fetch step yet. A bounded wait used as a negative-result
        # guard, not as the primary synchronization mechanism: if the lock
        # only covered fetch, B would sail through almost immediately.
        self.assertFalse(
            q_entered_fetch.wait(timeout=1.5),
            "B entered its provider fetch while A had not yet committed -- "
            "the lock does not span fetch-through-commit",
        )

        p_release_commit.set()
        self.assertTrue(p_committed.wait(timeout=10), "A must actually commit once released")
        p.join(timeout=10)
        self.assertFalse(p.is_alive(), "process A must not hang")

        self.assertTrue(
            q_entered_fetch.wait(timeout=10), "B must fetch once A has committed and released the lock"
        )
        q.join(timeout=10)
        self.assertFalse(q.is_alive(), "process B must not hang")

        results = [queue.get(timeout=10) for _ in range(2)]
        errors = [r for r in results if r[1] != "ok"]
        self.assertEqual(errors, [], f"both processes must succeed, got: {errors}")
        self.assertEqual({r[0] for r in results}, {"p", "q"}, "both processes must report in")

        conn = connect(self.cfg.db_path)
        try:
            row = conn.execute(
                "SELECT closed, needs_retriage, absence_streak FROM tasks WHERE id = ?",
                ("ticktick:A",),
            ).fetchone()
            self.assertEqual(
                row["absence_streak"],
                store.ABSENCE_CONFIRM_THRESHOLD,
                "two strictly serialized misses must land exactly on the confirm threshold, "
                "not be lost or double-counted by an overlapping read-modify-write",
            )
            self.assertEqual(row["closed"], 1, "the second serialized miss must confirm the departure")
            self.assertEqual(row["needs_retriage"], 1)

            plan_a = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertIsNotNone(plan_a, "closing must not delete A's plan row")
            self.assertEqual(plan_a["position"], 0)

            plan_b = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:B",)
            ).fetchone()
            self.assertEqual(plan_b["position"], 1)

            revision = store.get_order_revision(conn)
        finally:
            conn.close()

        self.assertEqual(
            revision,
            2,
            "1 for the initial set_order, 1 for A's confirmed departure -- exactly what "
            "strictly serialized execution (P's non-closing miss, then Q's closing miss) produces",
        )


if __name__ == "__main__":
    unittest.main()
