"""Configuration: env vars, with a couple of convenient fallbacks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("DAYPLAN_CONFIG_DIR", Path.home() / ".config" / "dayplan"))
DATA_DIR = Path(os.environ.get("DAYPLAN_DATA_DIR", Path.home() / ".local" / "share" / "dayplan"))

# Deliberately generic: statusCategory works on every Jira workflow, whereas
# named statuses are per-project. Override with JIRA_JQL to exclude the
# parked states your own workflow uses.
DEFAULT_JIRA_JQL = (
    "assignee = currentUser() AND statusCategory != Done "
    "ORDER BY priority DESC, due ASC, created ASC, project ASC"
)


def _load_env_file(path: Path) -> None:
    """Minimal .env loader. Existing environment variables always win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in os.environ.get(name, "").split(",") if v.strip())


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def load_env() -> None:
    """Load ./.env then ~/.config/dayplan/env, without clobbering the real env."""
    _load_env_file(Path.cwd() / ".env")
    _load_env_file(CONFIG_DIR / "env")


@dataclass(frozen=True)
class Config:
    db_path: Path
    ticktick_token: str | None
    ticktick_token_source: str
    asana_token: str | None
    asana_workspaces: tuple[str, ...]
    asana_projects: tuple[str, ...]
    asana_sections: tuple[str, ...]
    asana_only_mine: bool
    asana_include_subtasks: bool
    ticktick_due_within_days: int | None
    ticktick_include_undated: bool
    jira_base_url: str | None
    jira_site_url: str | None
    jira_email: str | None
    jira_api_token: str | None
    jira_jql: str
    sync_interval_minutes: int

    @property
    def jira_ready(self) -> bool:
        return bool(self.jira_base_url and self.jira_email and self.jira_api_token)

    def enabled_sources(self) -> list[str]:
        sources = []
        if self.ticktick_token:
            sources.append("ticktick")
        if self.asana_token:
            sources.append("asana")
        if self.jira_ready:
            sources.append("jira")
        return sources


def load_config() -> Config:
    load_env()

    ticktick_token = (
        os.environ.get("TICKTICK_TOKEN") or os.environ.get("TICKTICK_ACCESS_TOKEN") or None
    )
    source = "TICKTICK_TOKEN" if ticktick_token else "TICKTICK_TOKEN not set"

    workspaces = _csv_env("ASANA_WORKSPACES")
    projects = _csv_env("ASANA_PROJECTS")
    sections = _csv_env("ASANA_SECTIONS")

    within = os.environ.get("TICKTICK_DUE_WITHIN_DAYS", "").strip()
    try:
        due_within = int(within) if within else None
    except ValueError:
        due_within = None

    base_url = os.environ.get("JIRA_BASE_URL") or None
    if base_url:
        base_url = base_url.rstrip("/")

    # Scoped API tokens must be used against https://api.atlassian.com/ex/jira/<cloudId>
    # rather than the site URL. That gateway address is no good for building
    # human-facing /browse/ links, so keep the site URL separately. If it is
    # not set we ask Jira for it (GET /rest/api/3/serverInfo).
    site_url = os.environ.get("JIRA_SITE_URL") or None
    if site_url:
        site_url = site_url.rstrip("/")
    elif base_url and "api.atlassian.com" not in base_url:
        site_url = base_url

    default_db = DATA_DIR / "dayplan.sqlite"
    return Config(
        db_path=Path(os.environ.get("DAYPLAN_DB", default_db)).expanduser(),
        ticktick_token=ticktick_token,
        ticktick_token_source=source,
        asana_token=os.environ.get("ASANA_TOKEN") or None,
        asana_workspaces=workspaces,
        asana_projects=projects,
        asana_sections=sections,
        asana_only_mine=_bool_env("ASANA_ONLY_MINE", True),
        asana_include_subtasks=_bool_env("ASANA_INCLUDE_SUBTASKS", True),
        ticktick_due_within_days=due_within,
        ticktick_include_undated=_bool_env("TICKTICK_INCLUDE_UNDATED", True),
        jira_base_url=base_url,
        jira_site_url=site_url,
        jira_email=os.environ.get("JIRA_EMAIL") or None,
        jira_api_token=os.environ.get("JIRA_API_TOKEN") or None,
        jira_jql=os.environ.get("JIRA_JQL") or DEFAULT_JIRA_JQL,
        sync_interval_minutes=_int_env("DAYPLAN_SYNC_INTERVAL_MINUTES", 0),
    )
