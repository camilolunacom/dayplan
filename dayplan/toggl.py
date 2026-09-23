"""Map a synced task onto a Toggl project.

The Toggl Track extension's generic "DOM Integration" turns a `.toggl-root`
element into a real timer button, so dayplan needs no Toggl API token. The
project has to be given as a **name**, not an id. From the installed
extension (4.11.21), both the content script and the background handler do:

    case "resolve-project": {
      const { projectName: n, selectedWorkspaceId: r } = e.payload
      const d = Object.values(projects).filter(u => u.name === n)
      return d.find(u => u.workspace_id === r) ?? d[0]
    }

The payload carries only `projectName`. `data-project-id` is read by
dom-integration.js and handed to createTimerLink, where nothing consumes it —
a dead parameter. Verified live: id alone produced project_id null; adding
the name produced the right project.

Matching is exact string equality, and among identically named projects it
returns the first. That is a real hazard here: 283 active projects in this
account are called "Development - Website", one per client. So a rule only
gets a `toggl_project` when that name identifies exactly one project.
Otherwise leave it out — the timer runs with no project, which beats running
against an arbitrary client's project.

`toggl_project_id` is optional and carried through for reference only; it
does not affect what Toggl does.

Rules live in <config dir>/toggl-projects.json:

    {
      "jira": [
        {"parent": "ALHM-7", "toggl_project": "Maintenance: Tickets"},
        {"key_prefix": "TN", "toggl_project": "Development - Website -- Maintenance"}
      ]
    }

First matching rule wins, in file order, so put the specific ones (an epic)
above the broad ones (a project key).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("dayplan.toggl")

MATCHERS = ("project_id", "project_contains", "title_contains", "parent", "key_prefix", "tag")


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
        # Keys starting with _ are notes to the reader, not sources.
        if str(source).startswith("_") or not isinstance(entries, list):
            continue
        clean = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("toggl_project")
            raw_id = entry.get("toggl_project_id")
            try:
                project_id = int(raw_id) if raw_id is not None else None
            except (TypeError, ValueError):
                project_id = None
            matchers = {k: v for k, v in entry.items() if k in MATCHERS}
            if not matchers:
                log.warning("rule with no matcher, it would match everything: %r", entry)
                continue
            if not name:
                # Deliberate for projects whose Toggl name is not unique: the
                # task tracks without a project rather than against a guess.
                log.debug("rule with no toggl_project, will track unprojected: %r", entry)
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
    if "project_id" in rule:
        wanted = str(rule["project_id"])
        memberships = (getattr(task, "raw", None) or {}).get("memberships") or []
        project_ids = {
            str((membership.get("project") or {}).get("gid"))
            for membership in memberships
        }
        if wanted not in project_ids:
            return False
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
    """Return (project name, project id) for the first matching rule.

    The name is what Toggl acts on; the id is informational.
    """
    for rule in rules.get(task.source, []):
        if _matches(rule, task):
            return rule.get("toggl_project"), rule.get("toggl_project_id")
    return None, None
