"""Committed rules must send unique project names to the Toggl extension."""
import unittest
from pathlib import Path

from dayplan.providers.base import RemoteTask
from dayplan.toggl import load_rules, resolve

RULES = Path(__file__).resolve().parents[1] / "deploy/toggl-projects.example.json"


class TogglMappingTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules(RULES)

    def test_tkt_uses_confirmed_themekraft_name(self):
        for key in ("TKT-100", "TKT-104"):
            task = RemoteTask(source="jira", external_id=key, title="Test")
            self.assertEqual(resolve(task, self.rules),
                             ("Development - Themekraft Plugins", 205762591))

    def test_linnworks_matches_asana_project_id(self):
        task = RemoteTask(source="asana", external_id="test", title="Test",
                          project="Web Delivery",
                          raw={"memberships": [{"project": {"gid": "1205642154828787"}}]})
        self.assertEqual(resolve(task, self.rules),
                         ("Development - Linnworks Website", 218695668))

    def test_same_asana_project_name_is_not_enough(self):
        task = RemoteTask(source="asana", external_id="test", title="Test",
                          project="Web Delivery",
                          raw={"memberships": [{"project": {"gid": "other"}}]})
        self.assertEqual(resolve(task, self.rules), (None, None))

    def test_open_path_and_subtasks_use_confirmed_unique_name(self):
        for project in ("Open Path Webdev Requests", "Open Path Webdev Requests › Parent"):
            task = RemoteTask(source="asana", external_id="test", title="Test", project=project)
            self.assertEqual(resolve(task, self.rules), ("Development - OPC Websites", 197054431))

    def test_unrelated_rules_stay_unchanged(self):
        for prefix, name, pid in [("TN", "Development - Website -- Maintenance", 182545135),
                                  ("OTHER", None, None)]:
            task = RemoteTask(source="jira", external_id=prefix + "-1", title="Test")
            self.assertEqual(resolve(task, self.rules), (name, pid))


if __name__ == "__main__":
    unittest.main()
