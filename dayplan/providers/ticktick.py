"""TickTick, through the official Open API (developer.ticktick.com).

Read-only: we list projects, then pull each project's task data.

Known limitation of the Open API: the Inbox is not returned by
GET /open/v1/project, so Inbox tasks are invisible unless you pass the inbox
project id explicitly via TICKTICK_EXTRA_PROJECT_IDS.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..config import Config
from ..dates import iso_to_local_day
from .base import ProviderError, RemoteTask

API = "https://api.ticktick.com/open/v1"

# TickTick uses 0 / 1 / 3 / 5; we normalize to 0..3.
PRIORITY_MAP = {0: 0, 1: 1, 3: 2, 5: 3}


def _client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30.0,
    )


def _get(client: httpx.Client, path: str) -> Any:
    try:
        response = client.get(path)
    except httpx.HTTPError as exc:
        raise ProviderError(f"TickTick request failed: {exc}") from exc
    if response.status_code in (401, 403):
        raise ProviderError(
            "TickTick rejected the token (401/403). Refresh it with `ticktick auth login`."
        )
    if response.status_code >= 400:
        raise ProviderError(f"TickTick {path} returned HTTP {response.status_code}")
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(f"TickTick {path} returned non-JSON body") from exc


def _to_task(payload: dict[str, Any], project_name: str | None) -> RemoteTask:
    task_id = str(payload.get("id"))
    project_id = str(payload.get("projectId") or "")
    is_all_day = bool(payload.get("isAllDay"))
    due = iso_to_local_day(payload.get("dueDate"), all_day=is_all_day) or iso_to_local_day(
        payload.get("startDate"), all_day=is_all_day
    )
    url = f"https://ticktick.com/webapp/#p/{project_id}/tasks/{task_id}" if project_id else None
    notes = payload.get("content") or payload.get("desc") or None
    return RemoteTask(
        source="ticktick",
        external_id=task_id,
        title=(payload.get("title") or "(untitled)").strip(),
        url=url,
        project=project_name,
        status="open" if payload.get("status", 0) == 0 else "completed",
        priority=PRIORITY_MAP.get(int(payload.get("priority") or 0), 0),
        due=due,
        tags=[str(t) for t in (payload.get("tags") or [])],
        notes=notes,
        raw=payload,
    )


def fetch(cfg: Config) -> list[RemoteTask]:
    if not cfg.ticktick_token:
        raise ProviderError(
            "No TickTick token. Set TICKTICK_TOKEN, or run `ticktick auth login` to mint one."
        )

    extra = [
        p.strip()
        for p in os.environ.get("TICKTICK_EXTRA_PROJECT_IDS", "").split(",")
        if p.strip()
    ]

    tasks: list[RemoteTask] = []
    with _client(cfg.ticktick_token) as client:
        projects = _get(client, "/project") or []
        project_ids = [str(p.get("id")) for p in projects if p.get("id")]
        names = {str(p.get("id")): p.get("name") for p in projects}
        for project_id in extra:
            if project_id not in names:
                project_ids.append(project_id)
                names.setdefault(project_id, "Inbox")

        for project_id in project_ids:
            data = _get(client, f"/project/{project_id}/data")
            if not data:
                continue
            project_name = (data.get("project") or {}).get("name") or names.get(project_id)
            for item in data.get("tasks") or []:
                if item.get("status", 0) != 0:
                    continue  # completed / abandoned
                tasks.append(_to_task(item, project_name))
    return tasks
