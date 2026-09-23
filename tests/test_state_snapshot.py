"""The /api/state payload must come from one consistent point in time.

Before this fix, `ordered_tasks`, `new_tasks`, `summary`, `integrations` and
`get_order_revision` were each a separate autocommit read: a concurrent write
(another sync, another `/api/order` PUT) could commit in between them,
pairing a stale task list with a revision that describes a state the list
does not reflect. That defeats the stale-write rejection `/api/order` relies
on, since the client would read a revision newer than the list it got back.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dayplan import store
from dayplan.db import connect

from _support import TempDbCase, remote


class StateSnapshotTests(TempDbCase, unittest.TestCase):
    def test_state_snapshot_is_not_torn_by_a_concurrent_mutation(self):
        self.sync_with([remote("A"), remote("B")])
        conn = connect(self.cfg.db_path)
        try:
            store.set_order(conn, ["ticktick:A", "ticktick:B"])
            revision_before = store.get_order_revision(conn)
        finally:
            conn.close()

        mutated = {"done": False}
        original_new_tasks = store.new_tasks

        def mutate_then_read(inner_conn):
            # Simulates a second connection (another request, or a sync)
            # committing a plan/revision mutation between the moment the
            # snapshot read the ordered list and the moment it reads the
            # revision -- exactly the window the old, unwrapped reads left
            # open.
            if not mutated["done"]:
                mutated["done"] = True
                second = connect(self.cfg.db_path)
                try:
                    store.set_order(second, ["ticktick:B", "ticktick:A"])
                finally:
                    second.close()
            return original_new_tasks(inner_conn)

        conn = connect(self.cfg.db_path)
        try:
            with patch("dayplan.store.new_tasks", side_effect=mutate_then_read):
                snapshot = store.state_snapshot(conn, self.cfg)
        finally:
            conn.close()

        ordered_ids = [t["id"] for t in snapshot["tasks"]]
        self.assertEqual(
            ordered_ids,
            ["ticktick:A", "ticktick:B"],
            "the task list must reflect one single point in time",
        )
        self.assertEqual(
            snapshot["order_revision"],
            revision_before,
            "the revision must describe the same snapshot as the lists, not a "
            "mutation that happened while the snapshot was being assembled",
        )


if __name__ == "__main__":
    unittest.main()
