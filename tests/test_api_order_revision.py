"""RED/GREEN coverage for the /api/order optimistic-concurrency contract.

Uses the real FastAPI app with only the SQLite connection dependency
overridden to point at a throwaway database -- no network, no mocked routes.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from dayplan import api
from dayplan.db import connect

from _support import TempDbCase, remote


class ApiOrderRevisionTests(TempDbCase, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()

        def override_conn():
            conn = connect(self.cfg.db_path)
            try:
                yield conn
            finally:
                conn.close()

        api.app.dependency_overrides[api.get_conn] = override_conn
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app)

    def test_state_exposes_order_revision(self):
        self.sync_with([remote("A"), remote("B")])
        resp = self.client.get("/api/state")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("order_revision", resp.json())
        self.assertIsInstance(resp.json()["order_revision"], int)

    def test_put_order_requires_revision_and_bumps_on_success(self):
        self.sync_with([remote("A"), remote("B")])
        revision = self.client.get("/api/state").json()["order_revision"]

        resp = self.client.put(
            "/api/order",
            json={"ids": ["ticktick:A", "ticktick:B"], "revision": revision},
        )
        self.assertEqual(resp.status_code, 200)

        new_revision = self.client.get("/api/state").json()["order_revision"]
        self.assertGreater(new_revision, revision)

    def test_put_order_without_revision_is_rejected(self):
        self.sync_with([remote("A"), remote("B")])
        resp = self.client.put("/api/order", json={"ids": ["ticktick:A", "ticktick:B"]})
        self.assertEqual(resp.status_code, 400)

    def test_put_order_rejects_boolean_revisions(self):
        self.sync_with([remote("A"), remote("B")])
        revision = self.client.get("/api/state").json()["order_revision"]

        for bad_revision in (True, False):
            resp = self.client.put(
                "/api/order",
                json={"ids": ["ticktick:A", "ticktick:B"], "revision": bad_revision},
            )
            self.assertEqual(
                resp.status_code, 400, f"revision={bad_revision!r} must be rejected, not coerced"
            )

        final = self.client.get("/api/state").json()
        self.assertEqual(final["order_revision"], revision, "a rejected write must not bump anything")

    def test_stale_put_order_returns_409_and_reopened_task_stays_in_new(self):
        self.sync_with([remote("A"), remote("B")])
        stale_revision = self.client.get("/api/state").json()["order_revision"]

        # A's confirmed departure and return happen out from under this
        # revision -- exactly the race the stale drag must lose.
        self.sync_with([remote("B")])
        self.sync_with([remote("B")])
        self.sync_with([remote("A"), remote("B")])

        resp = self.client.put(
            "/api/order",
            json={"ids": ["ticktick:A", "ticktick:B"], "revision": stale_revision},
        )
        self.assertEqual(resp.status_code, 409)

        final = self.client.get("/api/state").json()
        new_ids = [t["id"] for t in final["new"]]
        self.assertIn("ticktick:A", new_ids, "the stale write must not have re-pinned A")
        ordered_ids = [t["id"] for t in final["tasks"]]
        self.assertNotIn("ticktick:A", ordered_ids)

    def test_unpin_endpoint_still_clears_position_without_dropping_the_row(self):
        self.sync_with([remote("A"), remote("B")])
        revision = self.client.get("/api/state").json()["order_revision"]
        self.client.put(
            "/api/order",
            json={"ids": ["ticktick:A", "ticktick:B"], "revision": revision},
        )

        resp = self.client.delete("/api/order/ticktick:A")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"task_id": "ticktick:A", "pinned": False})

        conn = connect(self.cfg.db_path)
        try:
            row = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "unpin must keep the task in the list")
        self.assertIsNone(row["position"])


if __name__ == "__main__":
    unittest.main()
