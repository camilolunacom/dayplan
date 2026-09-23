"""TDD suite for safe retriage of reopened tasks.

Each test corresponds to one behavior slice from the reopen-safety spec.
Run with: python -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import threading
import unittest

from dayplan import store
from dayplan.db import connect

from _support import TempDbCase, advancing_clock, remote


class ZeroTaskResponseTests(TempDbCase, unittest.TestCase):
    def test_empty_provider_result_leaves_absence_streak_and_revision_unchanged(self):
        self.sync_with([remote("A")])
        conn = connect(self.cfg.db_path)
        try:
            revision_before = store.get_order_revision(conn)
            streak_before = conn.execute(
                "SELECT absence_streak FROM tasks WHERE id = ?", ("ticktick:A",)
            ).fetchone()["absence_streak"]
        finally:
            conn.close()

        self.sync_with([])

        conn = connect(self.cfg.db_path)
        try:
            revision_after = store.get_order_revision(conn)
            streak_after = conn.execute(
                "SELECT absence_streak FROM tasks WHERE id = ?", ("ticktick:A",)
            ).fetchone()["absence_streak"]
            self.assertEqual(
                revision_after, revision_before, "an empty response must not bump the revision"
            )
            self.assertEqual(
                streak_after, streak_before, "an empty response must not advance any streak"
            )
        finally:
            conn.close()

    def test_zero_task_response_does_not_close_or_destroy_order(self):
        # Two tasks arrive, get hand-ordered.
        self.sync_with([remote("A"), remote("B")])
        conn = connect(self.cfg.db_path)
        try:
            store.set_order(conn, ["ticktick:A", "ticktick:B"])
        finally:
            conn.close()

        # The provider then has a hiccup and reports zero tasks, successfully.
        self.sync_with([])

        conn = connect(self.cfg.db_path)
        try:
            row_a = conn.execute(
                "SELECT closed FROM tasks WHERE id = ?", ("ticktick:A",)
            ).fetchone()
            row_b = conn.execute(
                "SELECT closed FROM tasks WHERE id = ?", ("ticktick:B",)
            ).fetchone()
            self.assertEqual(row_a["closed"], 0, "a zero-task response must not close A")
            self.assertEqual(row_b["closed"], 0, "a zero-task response must not close B")

            plan_a = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
            plan_b = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:B",)
            ).fetchone()
            self.assertIsNotNone(plan_a, "A's plan row must survive an empty response")
            self.assertEqual(plan_a["position"], 0)
            self.assertIsNotNone(plan_b, "B's plan row must survive an empty response")
            self.assertEqual(plan_b["position"], 1)
        finally:
            conn.close()


class PartialOmissionTests(TempDbCase, unittest.TestCase):
    def test_one_partial_omission_and_recovery_preserves_order(self):
        self.sync_with([remote("A"), remote("B")])
        conn = connect(self.cfg.db_path)
        try:
            store.set_order(conn, ["ticktick:A", "ticktick:B"])
        finally:
            conn.close()

        # A returns nothing for A this one time, but B (an "other task from
        # that source") is present, so this is a genuine partial omission.
        self.sync_with([remote("B")])

        conn = connect(self.cfg.db_path)
        try:
            row_a = conn.execute(
                "SELECT closed, absence_streak FROM tasks WHERE id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertEqual(row_a["closed"], 0, "one miss must not close A")
            self.assertEqual(row_a["absence_streak"], 1)
        finally:
            conn.close()

        # A comes back before a second consecutive miss.
        self.sync_with([remote("A"), remote("B")])

        conn = connect(self.cfg.db_path)
        try:
            row_a = conn.execute(
                "SELECT closed, absence_streak, needs_retriage FROM tasks WHERE id = ?",
                ("ticktick:A",),
            ).fetchone()
            self.assertEqual(row_a["closed"], 0)
            self.assertEqual(row_a["absence_streak"], 0, "being seen resets the streak")
            self.assertEqual(row_a["needs_retriage"], 0)

            plan_a = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
            plan_b = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:B",)
            ).fetchone()
            self.assertEqual(plan_a["position"], 0, "recovery must not move A")
            self.assertEqual(plan_b["position"], 1, "recovery must not move B")
        finally:
            conn.close()


class PartialOmissionThenEmptyResultTests(TempDbCase, unittest.TestCase):
    def test_partial_omission_then_empty_result_keeps_streak_at_one_and_stays_open(self):
        self.sync_with([remote("A"), remote("B")])
        self.sync_with([remote("B")])  # miss 1 for A, non-empty -> streak 1

        # An empty response must not count as a second miss.
        self.sync_with([])

        conn = connect(self.cfg.db_path)
        try:
            row = conn.execute(
                "SELECT closed, absence_streak FROM tasks WHERE id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertEqual(row["closed"], 0, "an empty response must not confirm departure")
            self.assertEqual(row["absence_streak"], 1, "an empty response must not advance the streak")
        finally:
            conn.close()


class AcknowledgedOnlyTaskSurvivesOmissionTests(TempDbCase, unittest.TestCase):
    def test_acknowledged_only_task_survives_one_omission_and_recovery(self):
        self.sync_with([remote("A"), remote("B")])
        conn = connect(self.cfg.db_path)
        try:
            store.acknowledge(conn, "ticktick:A")
        finally:
            conn.close()

        self.sync_with([remote("B")])  # A misses once, non-empty, partial
        self.sync_with([remote("A"), remote("B")])  # A returns before a second miss

        conn = connect(self.cfg.db_path)
        try:
            row = conn.execute(
                "SELECT closed, needs_retriage FROM tasks WHERE id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertEqual(row["closed"], 0)
            self.assertEqual(row["needs_retriage"], 0)

            plan_a = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertIsNotNone(plan_a, "the acknowledged row must survive a transient omission")
            self.assertIsNone(plan_a["position"], "it must stay unranked, not gain or lose a position")
        finally:
            conn.close()


class ConfirmedReopenFromAcknowledgedOnlyTests(TempDbCase, unittest.TestCase):
    def test_confirmed_departure_and_return_of_an_acknowledged_only_task(self):
        self.sync_with([remote("A"), remote("B")])
        conn = connect(self.cfg.db_path)
        try:
            store.acknowledge(conn, "ticktick:A")  # unranked, position NULL
            revision_before = store.get_order_revision(conn)
        finally:
            conn.close()

        self.sync_with([remote("B")])  # miss 1 for A, non-empty -> streak 1
        self.sync_with([remote("B")])  # miss 2 -> A confirmed departed

        conn = connect(self.cfg.db_path)
        try:
            row = conn.execute(
                "SELECT closed, needs_retriage FROM tasks WHERE id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertEqual(row["closed"], 1, "two straight non-empty misses must close it")
            plan_a = conn.execute(
                "SELECT 1 FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertIsNotNone(plan_a, "closing must not delete the acknowledged plan row")
        finally:
            conn.close()

        self.sync_with([remote("A"), remote("B")])  # A returns

        conn = connect(self.cfg.db_path)
        try:
            plan_a = conn.execute(
                "SELECT 1 FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertIsNone(plan_a, "the reopened task's acknowledged-only plan row must be gone")

            row = conn.execute(
                "SELECT closed, needs_retriage, reappeared_at FROM tasks WHERE id = ?",
                ("ticktick:A",),
            ).fetchone()
            self.assertEqual(row["closed"], 0)
            self.assertEqual(row["needs_retriage"], 0, "the transition state must be cleared")
            self.assertIsNotNone(row["reappeared_at"], "a recognized reopen must be timestamped")

            fresh_ids = [t["id"] for t in store.new_tasks(conn)]
            self.assertIn("ticktick:A", fresh_ids, "it must land back in New")

            ordered_ids = [t["id"] for t in store.ordered_tasks(conn)]
            self.assertNotIn(
                "ticktick:A", ordered_ids, "it must not still count as acknowledged/unordered"
            )

            revision_after = store.get_order_revision(conn)
            self.assertGreater(
                revision_after, revision_before, "the confirmed reopen must advance the revision"
            )
        finally:
            conn.close()


class SetOrderCrossConnectionAtomicityTests(TempDbCase, unittest.TestCase):
    def test_competing_writers_serialize_and_the_stale_one_is_rejected(self):
        self.sync_with([remote("A"), remote("B"), remote("C")])
        conn = connect(self.cfg.db_path)
        try:
            store.set_order(conn, ["ticktick:A", "ticktick:B", "ticktick:C"])
            revision = store.get_order_revision(conn)
        finally:
            conn.close()

        barrier = threading.Barrier(2)
        results: dict[str, tuple] = {}

        def attempt(name: str, ids: list[str]) -> None:
            conn = connect(self.cfg.db_path)
            try:
                barrier.wait(timeout=5)  # both writers race for the same revision
                try:
                    final = store.set_order(conn, ids, expected_revision=revision)
                    results[name] = ("ok", final)
                except store.ConflictError as exc:
                    results[name] = ("conflict", str(exc))
            finally:
                conn.close()

        t1 = threading.Thread(
            target=attempt, args=("one", ["ticktick:B", "ticktick:A", "ticktick:C"])
        )
        t2 = threading.Thread(
            target=attempt, args=("two", ["ticktick:C", "ticktick:A", "ticktick:B"])
        )
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        outcomes = sorted(outcome for outcome, _ in results.values())
        self.assertEqual(
            outcomes,
            ["conflict", "ok"],
            "exactly one writer must win the real cross-connection race",
        )

        winner_name = "one" if results["one"][0] == "ok" else "two"
        winner_ids = results[winner_name][1]

        conn = connect(self.cfg.db_path)
        try:
            positions = {
                row["task_id"]: row["position"]
                for row in conn.execute("SELECT task_id, position FROM plan").fetchall()
            }
        finally:
            conn.close()
        for index, task_id in enumerate(winner_ids):
            self.assertEqual(
                positions[task_id], index, "the final order must be exactly the winner's write"
            )


class ConfirmedDepartureTests(TempDbCase, unittest.TestCase):
    def test_two_consecutive_non_empty_omissions_confirm_departure(self):
        self.sync_with([remote("A"), remote("B")])
        conn = connect(self.cfg.db_path)
        try:
            store.set_order(conn, ["ticktick:A", "ticktick:B"])
        finally:
            conn.close()

        self.sync_with([remote("B")])  # miss 1, non-empty, B present
        self.sync_with([remote("B")])  # miss 2, non-empty, B present -> confirmed

        conn = connect(self.cfg.db_path)
        try:
            row_a = conn.execute(
                "SELECT closed, needs_retriage FROM tasks WHERE id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertEqual(row_a["closed"], 1, "two straight non-empty misses close it")
            self.assertEqual(row_a["needs_retriage"], 1, "flagged so its return is recognized")

            # The plan row -- and A's manual position -- must survive: a
            # confirmed close is not a delete.
            plan_a = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertIsNotNone(plan_a, "closing must not delete the plan row")
            self.assertEqual(plan_a["position"], 0)
        finally:
            conn.close()


class ReopenReturnsToNewTests(TempDbCase, unittest.TestCase):
    def test_confirmed_departure_then_return_lands_in_new_without_old_plan_row(self):
        self.sync_with([remote("A"), remote("B")])
        conn = connect(self.cfg.db_path)
        try:
            store.set_order(conn, ["ticktick:A", "ticktick:B"])
            revision_before = store.get_order_revision(conn)
        finally:
            conn.close()

        self.sync_with([remote("B")])  # miss 1
        self.sync_with([remote("B")])  # miss 2 -> A confirmed departed

        # A returns.
        self.sync_with([remote("A"), remote("B")])

        conn = connect(self.cfg.db_path)
        try:
            plan_a = conn.execute(
                "SELECT 1 FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertIsNone(plan_a, "the reopened task's old plan row must be gone")

            row_a = conn.execute(
                "SELECT closed, needs_retriage FROM tasks WHERE id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertEqual(row_a["closed"], 0)
            self.assertEqual(row_a["needs_retriage"], 0, "the transition state must be cleared")

            fresh_ids = [t["id"] for t in store.new_tasks(conn)]
            self.assertIn("ticktick:A", fresh_ids, "it must reappear in the New pile")

            ordered_ids = [t["id"] for t in store.ordered_tasks(conn)]
            self.assertNotIn(
                "ticktick:A", ordered_ids, "it must not keep its old spot in the ordered list"
            )

            revision_after = store.get_order_revision(conn)
            self.assertGreater(revision_after, revision_before, "the reopen must bump the revision")
        finally:
            conn.close()


class ReopenSortsByReappearanceTests(TempDbCase, unittest.TestCase):
    def test_reopened_task_sorts_by_reappearance_not_first_seen(self):
        with advancing_clock():
            # A arrives first, so its first_seen is the oldest timestamp here.
            self.sync_with([remote("A"), remote("B")])
            conn = connect(self.cfg.db_path)
            try:
                store.set_order(conn, ["ticktick:A", "ticktick:B"])
            finally:
                conn.close()

            self.sync_with([remote("B")])  # miss 1 for A
            self.sync_with([remote("B")])  # miss 2 -> A confirmed departed

            # C is a genuinely new arrival, seen well after A's original
            # first_seen but before A returns.
            self.sync_with([remote("B"), remote("C")])

            # A finally returns, after C already exists.
            self.sync_with([remote("A"), remote("B"), remote("C")])

        conn = connect(self.cfg.db_path)
        try:
            fresh = store.new_tasks(conn)
            fresh_ids = [t["id"] for t in fresh]
            self.assertIn("ticktick:A", fresh_ids)
            self.assertIn("ticktick:C", fresh_ids)
            self.assertEqual(
                fresh_ids.index("ticktick:A"),
                0,
                "A reappeared after C existed, so it must sort above C in New",
            )
        finally:
            conn.close()


class OrdinaryUpdateRegressionTests(TempDbCase, unittest.TestCase):
    def test_normal_update_to_open_ordered_task_preserves_position(self):
        self.sync_with([remote("A"), remote("B")])
        conn = connect(self.cfg.db_path)
        try:
            store.set_order(conn, ["ticktick:A", "ticktick:B"])
        finally:
            conn.close()

        # An ordinary re-sync: both tasks present, one with an edited title.
        self.sync_with([remote("A", title="A, renamed"), remote("B")])

        conn = connect(self.cfg.db_path)
        try:
            plan_a = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertEqual(plan_a["position"], 0, "an ordinary update must not move A")
            title = conn.execute(
                "SELECT title FROM tasks WHERE id = ?", ("ticktick:A",)
            ).fetchone()["title"]
            self.assertEqual(title, "A, renamed")
        finally:
            conn.close()

    def test_normal_update_to_acknowledged_only_task_preserves_null_position(self):
        self.sync_with([remote("A")])
        conn = connect(self.cfg.db_path)
        try:
            store.acknowledge(conn, "ticktick:A")
        finally:
            conn.close()

        self.sync_with([remote("A", title="A, renamed")])

        conn = connect(self.cfg.db_path)
        try:
            plan_a = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertIsNotNone(plan_a, "the acknowledged row must survive")
            self.assertIsNone(plan_a["position"], "acknowledged-only stays unranked, not pinned")
        finally:
            conn.close()


class OrderRevisionConcurrencyTests(TempDbCase, unittest.TestCase):
    def test_stale_revision_is_rejected_and_cannot_reposition_reopened_task(self):
        self.sync_with([remote("A"), remote("B")])
        conn = connect(self.cfg.db_path)
        try:
            store.set_order(conn, ["ticktick:A", "ticktick:B"])
            stale_revision = store.get_order_revision(conn)
        finally:
            conn.close()

        # A departs (confirmed) and returns, which bumps the revision and
        # drops A's old plan row -- exactly the write a stale drag must not
        # trample.
        self.sync_with([remote("B")])
        self.sync_with([remote("B")])
        self.sync_with([remote("A"), remote("B")])

        conn = connect(self.cfg.db_path)
        try:
            current_revision = store.get_order_revision(conn)
            self.assertNotEqual(
                stale_revision, current_revision, "the reopen must have moved the revision on"
            )

            with self.assertRaises(store.ConflictError):
                store.set_order(
                    conn,
                    ["ticktick:A", "ticktick:B"],
                    expected_revision=stale_revision,
                )

            # The stale write must not have gone through: A stays out of the
            # ordered list and keeps no plan row.
            plan_a = conn.execute(
                "SELECT 1 FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertIsNone(plan_a, "a rejected stale write must not recreate A's plan row")
        finally:
            conn.close()

    def test_matching_revision_is_accepted(self):
        self.sync_with([remote("A"), remote("B")])
        conn = connect(self.cfg.db_path)
        try:
            store.set_order(conn, ["ticktick:A"])
            revision = store.get_order_revision(conn)
            store.set_order(conn, ["ticktick:B", "ticktick:A"], expected_revision=revision)
            positions = {
                row["task_id"]: row["position"]
                for row in conn.execute("SELECT task_id, position FROM plan").fetchall()
            }
            self.assertEqual(positions["ticktick:B"], 0)
            self.assertEqual(positions["ticktick:A"], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
