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
        "tags.name",
        "assignee_status",
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


def fetch(cfg: Config) -> list[RemoteTask]:
    if not cfg.asana_token:
        raise ProviderError("No ASANA_TOKEN set. Create one at https://app.asana.com/0/my-apps")

    tasks: list[RemoteTask] = []
    with _client(cfg.asana_token) as client:
        me = _get(client, "/users/me", {"opt_fields": "gid,name,workspaces.gid,workspaces.name"})
        workspaces = (me.get("data") or {}).get("workspaces") or []
        if cfg.asana_workspaces:
            wanted = set(cfg.asana_workspaces)
            workspaces = [w for w in workspaces if str(w.get("gid")) in wanted]
        if not workspaces:
            raise ProviderError("The Asana token sees no workspaces (check ASANA_WORKSPACES).")

        for workspace in workspaces:
            gid = str(workspace.get("gid"))
            rows = _paginate(
                client,
                "/tasks",
                {
                    "assignee": "me",
                    "workspace": gid,
                    "completed_since": "now",
                    "opt_fields": TASK_FIELDS,
                },
            )
            for row in rows:
                if row.get("completed"):
                    continue
                tasks.append(_to_task(row, workspace.get("name")))
    return tasks
