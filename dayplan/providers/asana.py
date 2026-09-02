"""Asana, through the REST API with a Personal Access Token.

Read-only: tasks assigned to the token owner and not yet completed, across
every workspace the token can see (or only those in ASANA_WORKSPACES).

Get a token at https://app.asana.com/0/my-apps -> Personal access token.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import Config
from ..dates import iso_to_local_day
from .base import ProviderError, RemoteTask

API = "https://app.asana.com/api/1.0"

TASK_FIELDS = ",".join(
    [
        "name",
        "completed",
        "due_on",
        "due_at",
        "permalink_url",
        "notes",
        "memberships.project.name",
        "memberships.section.name",
        "tags.name",
        "assignee.gid",
        "num_subtasks",
    ]
)

SUBTASK_FIELDS = ",".join(
    [
        "name",
        "completed",
        "due_on",
        "due_at",
        "permalink_url",
        "notes",
        "tags.name",
        "assignee.gid",
        "num_subtasks",
    ]
)


def _client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30.0,
    )


def _get(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        response = client.get(path, params=params)
    except httpx.HTTPError as exc:
        raise ProviderError(f"Asana request failed: {exc}") from exc
    if response.status_code in (401, 403):
        raise ProviderError(
            "Asana rejected the token (401/403). Check ASANA_TOKEN at https://app.asana.com/0/my-apps"
        )
    if response.status_code == 429:
        raise ProviderError("Asana rate limited the request (429). Try again in a minute.")
    if response.status_code >= 400:
        detail = ""
        try:
            errors = response.json().get("errors") or []
            detail = "; ".join(e.get("message", "") for e in errors)
        except ValueError:
            pass
        raise ProviderError(f"Asana {path} returned HTTP {response.status_code} {detail}".strip())
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(f"Asana {path} returned non-JSON body") from exc


def _paginate(client: httpx.Client, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_params = dict(params)
    page_params["limit"] = 100
    while True:
        payload = _get(client, path, page_params)
        items.extend(payload.get("data") or [])
        next_page = payload.get("next_page")
        if not next_page or not next_page.get("offset"):
            return items
        page_params["offset"] = next_page["offset"]


def _project_name(payload: dict[str, Any]) -> str | None:
    for membership in payload.get("memberships") or []:
        project = membership.get("project") or {}
        if project.get("name"):
            return project["name"]
    return None


def _section_name(payload: dict[str, Any]) -> str | None:
    for membership in payload.get("memberships") or []:
        section = membership.get("section") or {}
        if section.get("name"):
            return section["name"]
    return None


def _assignee_gid(payload: dict[str, Any]) -> str | None:
    return (payload.get("assignee") or {}).get("gid")


def _to_task(payload: dict[str, Any], workspace_name: str | None) -> RemoteTask:
    project = _project_name(payload) or workspace_name
    due = payload.get("due_on") or iso_to_local_day(payload.get("due_at"))
    return RemoteTask(
        source="asana",
        external_id=str(payload.get("gid")),
        title=(payload.get("name") or "(untitled)").strip(),
        url=payload.get("permalink_url"),
        project=project,
        status="open",
        priority=0,  # Asana priority is a custom field; not read in v1
        due=due,
        tags=[t.get("name") for t in (payload.get("tags") or []) if t.get("name")],
        notes=(payload.get("notes") or None),
        raw=payload,
    )


def _collect_subtasks(
    client: httpx.Client,
    parent: dict[str, Any],
    parent_task: RemoteTask,
    my_gid: str | None,
    cfg: Config,
    out: list[RemoteTask],
) -> None:
    """Pull the open subtasks of a matching task.

    Subtasks are not project members, so they never appear in a
    /tasks?project= listing and no section filter applies to them: they are
    included because their parent matched. A subtask assigned to somebody else
    is theirs, not his, so it is dropped -- but an unassigned subtask under his
    parent is kept.
    """
    if not cfg.asana_include_subtasks or not parent.get("num_subtasks"):
        return
    rows = _paginate(
        client, f"/tasks/{parent.get('gid')}/subtasks", {"opt_fields": SUBTASK_FIELDS}
    )
    for row in rows:
        if row.get("completed"):
            continue
        assignee = _assignee_gid(row)
        if assignee and my_gid and assignee != my_gid:
            continue
        task = _to_task(row, parent_task.project)
        # Carry the parent so a subtask does not read as an orphan item.
        task.project = f"{parent_task.project} › {parent.get('name')}"
        out.append(task)
        _collect_subtasks(client, row, task, my_gid, cfg, out)


def _fetch_projects(client: httpx.Client, cfg: Config, my_gid: str | None) -> list[RemoteTask]:
    """Scoped mode: only the listed projects, optionally only some sections."""
    wanted_sections = {s.lower() for s in cfg.asana_sections}
    tasks: list[RemoteTask] = []
    for project_gid in cfg.asana_projects:
        info = _get(client, f"/projects/{project_gid}", {"opt_fields": "name"})
        project_name = (info.get("data") or {}).get("name") or project_gid

        rows = _paginate(
            client,
            "/tasks",
            {"project": project_gid, "completed_since": "now", "opt_fields": TASK_FIELDS},
        )
        for row in rows:
            if row.get("completed"):
                continue
            if wanted_sections and (_section_name(row) or "").lower() not in wanted_sections:
                continue
            if cfg.asana_only_mine and _assignee_gid(row) != my_gid:
                continue
            task = _to_task(row, project_name)
            tasks.append(task)
            _collect_subtasks(client, row, task, my_gid, cfg, tasks)
    return tasks


def _fetch_workspaces(client: httpx.Client, cfg: Config, my_gid: str | None,
                      workspaces: list[dict[str, Any]]) -> list[RemoteTask]:
    """Default mode: everything assigned to the token owner, per workspace."""
    tasks: list[RemoteTask] = []
    for workspace in workspaces:
        rows = _paginate(
            client,
            "/tasks",
            {
                "assignee": "me",
                "workspace": str(workspace.get("gid")),
                "completed_since": "now",
                "opt_fields": TASK_FIELDS,
            },
        )
        for row in rows:
            if row.get("completed"):
                continue
            task = _to_task(row, workspace.get("name"))
            tasks.append(task)
            _collect_subtasks(client, row, task, my_gid, cfg, tasks)
    return tasks


def fetch(cfg: Config) -> list[RemoteTask]:
    if not cfg.asana_token:
        raise ProviderError("No ASANA_TOKEN set. Create one at https://app.asana.com/0/my-apps")

    with _client(cfg.asana_token) as client:
        me = _get(client, "/users/me", {"opt_fields": "gid,name,workspaces.gid,workspaces.name"})
        me_data = me.get("data") or {}
        my_gid = me_data.get("gid")

        # ASANA_PROJECTS narrows Asana to those projects only; without it we
        # fall back to everything assigned to you across the workspaces.
        if cfg.asana_projects:
            return _dedupe(_fetch_projects(client, cfg, my_gid))

        workspaces = me_data.get("workspaces") or []
        if cfg.asana_workspaces:
            wanted = set(cfg.asana_workspaces)
            workspaces = [w for w in workspaces if str(w.get("gid")) in wanted]
        if not workspaces:
            raise ProviderError("The Asana token sees no workspaces (check ASANA_WORKSPACES).")
        return _dedupe(_fetch_workspaces(client, cfg, my_gid, workspaces))


def _dedupe(tasks: list[RemoteTask]) -> list[RemoteTask]:
    """A subtask assigned to you also comes back from the assignee query."""
    seen: set[str] = set()
    out = []
    for task in tasks:
        if task.external_id in seen:
            continue
        seen.add(task.external_id)
        out.append(task)
    return out
