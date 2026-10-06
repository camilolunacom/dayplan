"""Every plan-mutating path must advance `order_revision`, not just
`set_order`.

`/api/order`'s stale-write rejection only protects a drag that started
before some *other* change moved the order out from under it -- and that
other change does not have to be another drag. Acknowledging a new task,
dismissing it back to New, and unpinning a ranked task all change what a
previously-read order revision describes. If any of those forgot to bump the
revision, a client could hold a drag started before the mutation, submit it
after, and clobber the mutation instead of being rejected with 409.

`assign` (single-task manual positioning) has no HTTP endpoint of its own --
it is only reachable at the store layer today -- so its invalidation is
proven directly against `store.assign`/`store.set_order` and
`store.ConflictError` instead of through the API.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from dayplan import store
from dayplan.db import connect

from _support import TempDbCase, remote


class ApiMutationRevisionInvalidationTests(TempDbCase, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()

        def override_conn():
            conn = connect(self.cfg.db_path)
            try:
                yield conn
            finally:
                conn.close()

        from dayplan import api

        api.app.dependency_overrides[api.get_conn] = override_conn
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app)

    def _revision(self) -> int:
        return self.client.get("/api/state").json()["order_revision"]

    def test_accept_bumps_revision_and_invalidates_a_stale_order_submission(self):
        self.sync_with([remote("A"), remote("B")])
        stale_revision = self._revision()

        resp = self.client.post("/api/accept", json={"task_id": "ticktick:A"})
        self.assertEqual(resp.status_code, 200)

        new_revision = self._revision()
        self.assertGreater(new_revision, stale_revision, "accept must bump the revision")

        put = self.client.put(
            "/api/order",
            json={"ids": ["ticktick:A", "ticktick:B"], "revision": stale_revision},
        )
        self.assertEqual(put.status_code, 409, "a drag started before the accept must be rejected")

        final = self.client.get("/api/state").json()
        ordered_ids = [t["id"] for t in final["tasks"]]
        self.assertIn("ticktick:A", ordered_ids, "the accept must not have been undone")
        pinned = {t["id"]: t["pinned"] for t in final["tasks"]}
        self.assertFalse(pinned["ticktick:A"], "accept must leave the task unranked, not pinned")

    def test_repeated_accept_is_a_no_op_and_does_not_bump_revision(self):
        self.sync_with([remote("A")])
        self.client.post("/api/accept", json={"task_id": "ticktick:A"})
        revision = self._revision()

        resp = self.client.post("/api/accept", json={"task_id": "ticktick:A"})
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(
            self._revision(), revision, "accepting an already-acknowledged task must be a no-op"
        )

    def test_dismiss_bumps_revision_and_invalidates_a_stale_order_submission(self):
        self.sync_with([remote("A"), remote("B")])
        self.client.post("/api/accept", json={"task_id": "ticktick:A"})
        stale_revision = self._revision()

        resp = self.client.post("/api/dismiss", json={"task_id": "ticktick:A"})
        self.assertEqual(resp.status_code, 200)

        new_revision = self._revision()
        self.assertGreater(new_revision, stale_revision, "dismiss must bump the revision")

        put = self.client.put(
            "/api/order",
            json={"ids": ["ticktick:A", "ticktick:B"], "revision": stale_revision},
        )
        self.assertEqual(put.status_code, 409, "a drag started before the dismiss must be rejected")

        final = self.client.get("/api/state").json()
        new_ids = [t["id"] for t in final["new"]]
        self.assertIn("ticktick:A", new_ids, "the dismissed task must stay in New")
        ordered_ids = [t["id"] for t in final["tasks"]]
        self.assertNotIn("ticktick:A", ordered_ids, "the dismiss must not have been undone")

    def test_dismiss_of_a_never_acknowledged_task_is_a_no_op_and_does_not_bump_revision(self):
        self.sync_with([remote("A")])
        revision = self._revision()

        resp = self.client.post("/api/dismiss", json={"task_id": "ticktick:A"})
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(
            self._revision(), revision, "dismissing a task with no plan row must be a no-op"
        )

    def test_unpin_bumps_revision_and_invalidates_a_stale_order_submission(self):
        self.sync_with([remote("A"), remote("B")])
        revision = self._revision()
        self.client.put(
            "/api/order",
            json={"ids": ["ticktick:A", "ticktick:B"], "revision": revision},
        )
        stale_revision = self._revision()

        resp = self.client.delete("/api/order/ticktick:A")
        self.assertEqual(resp.status_code, 200)

        new_revision = self._revision()
        self.assertGreater(new_revision, stale_revision, "unpin must bump the revision")

        put = self.client.put(
            "/api/order",
            json={"ids": ["ticktick:A", "ticktick:B"], "revision": stale_revision},
        )
        self.assertEqual(put.status_code, 409, "a drag started before the unpin must be rejected")

        final = self.client.get("/api/state").json()
        by_id = {t["id"]: t for t in final["tasks"]}
        self.assertFalse(by_id["ticktick:A"]["pinned"], "the unpinned task must stay unranked")
        self.assertIsNone(by_id["ticktick:A"]["position"])

    def test_unpin_of_a_never_pinned_task_is_a_no_op_and_does_not_bump_revision(self):
        self.sync_with([remote("A")])
        self.client.post("/api/accept", json={"task_id": "ticktick:A"})  # unordered, no position
        revision = self._revision()

        resp = self.client.delete("/api/order/ticktick:A")
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(
            self._revision(), revision, "unpinning a task that carries no position must be a no-op"
        )


class StoreAssignRevisionInvalidationTests(TempDbCase, unittest.TestCase):
    """`store.assign` has no HTTP endpoint today, so its invalidation is
    proven at the store layer with `store.ConflictError`, mirroring the
    `/api/order` 409 contract those tests use."""

    def test_assign_bumps_revision_and_invalidates_a_stale_set_order(self):
        self.sync_with([remote("A"), remote("B"), remote("C")])
        conn = connect(self.cfg.db_path)
        try:
            stale_revision = store.get_order_revision(conn)

            store.assign(conn, "ticktick:A", 0)
            new_revision = store.get_order_revision(conn)
            self.assertGreater(new_revision, stale_revision, "assign must bump the revision")

            with self.assertRaises(store.ConflictError):
                store.set_order(
                    conn, ["ticktick:B", "ticktick:C"], expected_revision=stale_revision
                )

            # The rejected stale write must not have undone the assign.
            plan_a = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
            self.assertIsNotNone(plan_a)
            self.assertEqual(plan_a["position"], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
