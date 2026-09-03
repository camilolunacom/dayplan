"""Map a synced task onto a Toggl project id.

The Toggl Track extension's generic "DOM Integration" reads `data-project-id`
off the element it turns into a button, so if we resolve the id here the
button lands in the right Toggl project with no API call from us.

Resolution happens at sync time rather than in the browser: the rules need
the Jira epic and the Asana project, and doing it once per sync keeps the
frontend free of provider knowledge. Change the rules and re-sync.

**Send the project NAME.** Every integration the extension ships passes
`projectName` and none passes `projectId`, so the core resolves projects by
name; a numeric id alone silently produces a timer with no project. We send
both when both are known, since the name is what works and the id costs
nothing.

Rules live in <config dir>/toggl-projects.json. `toggl_project` is the name
as it appears in Toggl; `toggl_project_id` is optional:

    {
      "asana": [
        {"project_contains": "Open Path",
         "toggl_project": "Open Path", "toggl_project_id": 197054431}
      ],
      "jira": [
        {"parent": "ALHM-7", "toggl_project": "ALHM Epic 7"},
        {"key_prefix": "TN", "toggl_project": "Support - TS"}
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
            name = entry.get("toggl_project")
            raw_id = entry.get("toggl_project_id")
            project_id: int | None
            try:
                project_id = int(raw_id) if raw_id is not None else None
            except (TypeError, ValueError):
                log.warning("rule with an unusable toggl_project_id: %r", entry)
                project_id = None
            if not name and project_id is None:
                log.warning("rule names no Toggl project: %r", entry)
                continue
            if not name:
                log.warning(
                    "rule %r has only an id; the extension resolves projects by name, "
                    "so add toggl_project",
                    entry,
                )
            matchers = {k: v for k, v in entry.items() if k in MATCHERS}
            if not matchers:
                log.warning("rule with no matcher, it would match everything: %r", entry)
                continue
            clean.append(
                {
                    "toggl_project": str(name) if name else None,
                    "toggl_project_id": project_id,
                    **matchers,
                }
            )
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


def resolve(task: Any, rules: dict[str, list[dict[str, Any]]]) -> tuple[str | None, int | None]:
    """Return (project name, project id) for the first matching rule."""
    for rule in rules.get(task.source, []):
        if _matches(rule, task):
            return rule.get("toggl_project"), rule.get("toggl_project_id")
    return None, None
