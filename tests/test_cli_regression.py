"""CLI regression coverage: `order`, `keep`, `dismiss`, `unpin` must keep
working unchanged after the reopen-safety rewrite of store.py."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from dayplan import store
from dayplan.cli import app as cli_app
from dayplan.db import connect

from _support import TempDbCase, remote


class CliRegressionTests(TempDbCase, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.runner = CliRunner()
        self._env_patch = patch.dict(
            os.environ,
            {
                "DAYPLAN_DB": str(self.cfg.db_path),
                "DAYPLAN_CONFIG_DIR": str(self.tmp / "config"),
                "DAYPLAN_DATA_DIR": str(self.tmp / "data"),
                "TICKTICK_TOKEN": "test-token",
            },
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_order_command_pins_tasks(self):
        self.sync_with([remote("A"), remote("B"), remote("C")])
        result = self.runner.invoke(cli_app, ["order", "ticktick:B", "ticktick:A"])
        self.assertEqual(result.exit_code, 0, result.output)

        conn = connect(self.cfg.db_path)
        try:
            positions = {
                row["task_id"]: row["position"]
                for row in conn.execute("SELECT task_id, position FROM plan").fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(positions["ticktick:B"], 0)
        self.assertEqual(positions["ticktick:A"], 1)

    def test_keep_then_dismiss_roundtrip(self):
        self.sync_with([remote("A")])

        result = self.runner.invoke(cli_app, ["keep", "ticktick:A"])
        self.assertEqual(result.exit_code, 0, result.output)
        conn = connect(self.cfg.db_path)
        try:
            row = conn.execute(
                "SELECT position FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "keep must create a plan row")
        self.assertIsNone(row["position"], "keep must not rank it")

        result = self.runner.invoke(cli_app, ["dismiss", "ticktick:A"])
        self.assertEqual(result.exit_code, 0, result.output)
        conn = connect(self.cfg.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM plan WHERE task_id = ?", ("ticktick:A",)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row, "dismiss must drop the plan row, back to New")

    def test_unpin_clears_position_without_dropping_the_row(self):
        self.sync_with([remote("A"), remote("B")])
        conn = connect(self.cfg.db_path)
        try:
            store.set_order(conn, ["ticktick:A", "ticktick:B"])
        finally:
            conn.close()

        result = self.runner.invoke(cli_app, ["unpin", "ticktick:A"])
        self.assertEqual(result.exit_code, 0, result.output)

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
