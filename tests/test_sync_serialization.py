"""Overlapping syncs must not interleave.

`store.sync` used to fetch from providers before any serialization existed:
a scheduled sync, a manual `/api/sync` call, and an external CLI `dayplan
sync` could all be mid-fetch at once, and whichever committed its
reconciliation last would win regardless of which snapshot was actually
newer -- a later-started sync could confirm a departure, and then an
earlier-started one could resume and spuriously reopen or delete plan state.

This proves the fix with a rendezvous barrier instead of sleeps: if two
`sync()` calls are ever inside the provider-fetch step at the same time, both
threads reach the barrier together and it releases normally. If the fetch
step is properly serialized behind a lock that spans fetch-through-commit,
the second call cannot even reach the barrier until the first call's entire
`sync()` -- fetch and DB apply -- has finished, so the first thread always
times out waiting alone.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from dayplan import store
from dayplan.db import connect

from _support import TempDbCase, remote


class SyncSerializationTests(TempDbCase, unittest.TestCase):
    def test_concurrent_syncs_do_not_overlap_their_provider_fetch(self):
        # Migrate the database up front: concurrent first-connect migration
        # is a separate, already-covered race (see test_migration.py). This
        # test isolates the sync-serialization behavior only.
        connect(self.cfg.db_path).close()

        barrier = threading.Barrier(2)
        results: dict[str, str] = {}

        def fake_fetch_all(cfg, sources):
            name = threading.current_thread().name
            try:
                barrier.wait(timeout=0.5)
                results[name] = "concurrent"
            except threading.BrokenBarrierError:
                results[name] = "serialized"
            return ({"ticktick": [remote("X")]}, {})

        with patch("dayplan.store.fetch_all", side_effect=fake_fetch_all):
            threads = [
                threading.Thread(
                    target=store.sync,
                    args=(self.cfg, ["ticktick"]),
                    kwargs={"trigger": "cli"},
                    name=name,
                )
                for name in ("A", "B")
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
                self.assertFalse(t.is_alive(), "sync() must not deadlock")

        self.assertEqual(
            results.get("A"), "serialized", "A's fetch must never overlap B's"
        )
        self.assertEqual(
            results.get("B"), "serialized", "B's fetch must never overlap A's"
        )


if __name__ == "__main__":
    unittest.main()
