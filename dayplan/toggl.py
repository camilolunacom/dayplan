"""Map a synced task onto a Toggl project id.

The Toggl Track extension's generic "DOM Integration" reads `data-project-id`
off the element it turns into a button, so if we resolve the id here the
button lands in the right Toggl project with no API call from us.

Resolution happens at sync time rather than in the browser: the rules need
the Jira epic and the Asana project, and doing it once per sync keeps the
frontend free of provider knowledge. Change the rules and re-sync.

Rules live in <config dir>/toggl-projects.json:

    {
      "asana": [
        {"project_contains": "Open Path", "toggl_project_id": 197054431}
      ],
      "jira": [
        {"parent": "ALHM-7",  "toggl_project_id": 209356898},
        {"key_prefix": "TN",  "toggl_project_id": 182545135}
      ]
    }

First matching rule wins, in file order, so put the specific ones (an epic)
above the broad ones (a project key). Anything unmatched gets no project,
which is what Toggl treats as "no project".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("dayplan.toggl")

MATCHERS = ("project_contains", "title_contains", "parent", "key_prefix", "tag")


def load_rules(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read the rules file. A missing file simply means no mapping."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("ignoring %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        log.warning("ignoring %s: expected an object keyed by source", path)
        return {}

    rules: dict[str, list[dict[str, Any]]] = {}
    for source, entries in data.items():
        if not isinstance(entries, list):
            continue
        clean = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                project_id = int(entry["toggl_project_id"])
            except (KeyError, TypeError, ValueError):
                log.warning("rule without a usable toggl_project_id: %r", entry)
                continue
            matchers = {k: v for k, v in entry.items() if k in MATCHERS}
            if not matchers:
                log.warning("rule with no matcher, it would match everything: %r", entry)
                continue
            clean.append({"toggl_project_id": project_id, **matchers})
        rules[str(source)] = clean
    return rules


def _matches(rule: dict[str, Any], task: Any) -> bool:
    """Every matcher present in the rule has to hold."""
    if "project_contains" in rule:
        needle = str(rule["project_contains"]).lower()
        if needle not in (task.project or "").lower():
            return False
    if "title_contains" in rule:
        if str(rule["title_contains"]).lower() not in (task.title or "").lower():
            return False
    if "parent" in rule:
        if str(rule["parent"]).lower() != str(task.parent or "").lower():
            return False
    if "key_prefix" in rule:
        prefix = str(task.external_id or "").split("-")[0]
        if prefix.lower() != str(rule["key_prefix"]).lower():
            return False
    if "tag" in rule:
        wanted = str(rule["tag"]).lower()
        if wanted not in {str(t).lower() for t in (task.tags or [])}:
            return False
    return True


def resolve(task: Any, rules: dict[str, list[dict[str, Any]]]) -> int | None:
    for rule in rules.get(task.source, []):
        if _matches(rule, task):
            return int(rule["toggl_project_id"])
    return None
