"""Jira Cloud, through /rest/api/3/search/jql with an API token.

Read-only. Uses the POST /search/jql endpoint: the old GET /rest/api/3/search
was removed from Jira Cloud in 2025, so anything built on it returns 410.

Both kinds of Atlassian API token work, and both authenticate the same way
(HTTP Basic, email:token) -- only the base URL differs:

  * A token WITHOUT scopes is a password replacement with your full account
    permissions. Set JIRA_BASE_URL to the site, https://your-site.atlassian.net
  * A token WITH scopes must go through the gateway instead:
    JIRA_BASE_URL=https://api.atlassian.com/ex/jira/<cloudId>
    It needs read:jira-work (the JQL search) and read:jira-user (/myself).

The gateway address cannot build /browse/ links, so when JIRA_BASE_URL points
at it we resolve the real site URL from /serverInfo (or JIRA_SITE_URL).

Tokens: https://id.atlassian.com/manage-profile/security/api-tokens
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from ..config import Config
from .base import ProviderError, RemoteTask

FIELDS = ["summary", "status", "priority", "duedate", "project", "labels", "parent", "updated"]

PRIORITY_MAP = {
    "highest": 3,
    "high": 3,
    "medium": 2,
    "low": 1,
    "lowest": 0,
}


def _client(cfg: Config) -> httpx.Client:
    raw = f"{cfg.jira_email}:{cfg.jira_api_token}".encode()
    token = base64.b64encode(raw).decode()
    return httpx.Client(
        base_url=f"{cfg.jira_base_url}/rest/api/3",
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )


def _site_url(client: httpx.Client, cfg: Config) -> str:
    """Where a human should click through to. Asked once per sync."""
    if cfg.jira_site_url:
        return cfg.jira_site_url
    try:
        response = client.get("/serverInfo")
        if response.status_code < 400:
            resolved = (response.json().get("baseUrl") or "").rstrip("/")
            if resolved:
                return resolved
    except (httpx.HTTPError, ValueError):
        pass
    return ""  # links get omitted rather than pointing somewhere wrong


def _to_task(issue: dict[str, Any], base_url: str) -> RemoteTask:
    fields = issue.get("fields") or {}
    key = str(issue.get("key"))
    priority_name = ((fields.get("priority") or {}).get("name") or "").strip().lower()
    project = (fields.get("project") or {}).get("name") or (fields.get("project") or {}).get("key")
    parent = (fields.get("parent") or {}).get("key")
    return RemoteTask(
        source="jira",
        external_id=key,
        title=f"{key} {(fields.get('summary') or '(untitled)').strip()}",
        url=f"{base_url}/browse/{key}" if base_url else None,
        project=project,
        status=(fields.get("status") or {}).get("name"),
        priority=PRIORITY_MAP.get(priority_name, 0),
        due=fields.get("duedate") or None,
        tags=[str(label) for label in (fields.get("labels") or [])],
        notes=f"parent: {parent}" if parent else None,
        raw=issue,
    )


def _assert_authenticated(client: httpx.Client) -> None:
    """Fail loudly on bad credentials.

    /search/jql answers HTTP 200 with an empty issue list when the credentials
    are rejected: it silently treats you as anonymous, and
    `assignee = currentUser()` then matches nothing. That is indistinguishable
    from "you have no work assigned". /myself does return 401, so ask it first.
    """
    try:
        response = client.get("/myself")
    except httpx.HTTPError as exc:
        raise ProviderError(f"Jira request failed: {exc}") from exc
    if response.status_code in (401, 403):
        raise ProviderError(
            "Jira rejected the credentials (401/403). Check JIRA_EMAIL and JIRA_API_TOKEN. "
            "A scoped token needs read:jira-work and read:jira-user, and JIRA_BASE_URL "
            "must be https://api.atlassian.com/ex/jira/<cloudId> rather than the site URL."
        )
    if response.status_code >= 400:
        raise ProviderError(f"Jira /myself returned HTTP {response.status_code}")


def fetch(cfg: Config) -> list[RemoteTask]:
    if not cfg.jira_ready:
        raise ProviderError(
            "Jira needs JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN. "
            "Token: https://id.atlassian.com/manage-profile/security/api-tokens"
        )

    tasks: list[RemoteTask] = []
    next_token: str | None = None
    with _client(cfg) as client:
        _assert_authenticated(client)
        site = _site_url(client, cfg)
        while True:
            body: dict[str, Any] = {
                "jql": cfg.jira_jql,
                "fields": FIELDS,
                "maxResults": 100,
            }
            if next_token:
                body["nextPageToken"] = next_token
            try:
                response = client.post("/search/jql", json=body)
            except httpx.HTTPError as exc:
                raise ProviderError(f"Jira request failed: {exc}") from exc
            if response.status_code in (401, 403):
                raise ProviderError(
                    "Jira rejected the credentials (401/403). Check JIRA_EMAIL and JIRA_API_TOKEN."
                )
            if response.status_code == 400:
                raise ProviderError(f"Jira refused the JQL: {response.text[:300]}")
            if response.status_code >= 400:
                raise ProviderError(
                    f"Jira /search/jql returned HTTP {response.status_code}: {response.text[:200]}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderError("Jira /search/jql returned non-JSON body") from exc

            for issue in payload.get("issues") or []:
                tasks.append(_to_task(issue, site))

            next_token = payload.get("nextPageToken")
            if payload.get("isLast") or not next_token:
                return tasks
