"""Shared fixtures for the reopen-safety regression suite.

Not itself a test module (no test_ prefix), so `unittest discover` skips it.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from dayplan.config import Config
from dayplan.providers.base import RemoteTask


@contextmanager
def advancing_clock(start: str = "2026-01-01T00:00:00+00:00", step_seconds: int = 1):
    """Patch dayplan.store._now so timestamps strictly increase, one tick per
    call, instead of racing real wall-clock second resolution in a fast test."""
    base = datetime.fromisoformat(start)
    counter = {"n": 0}

    def _next() -> str:
        counter["n"] += 1
        return (base + timedelta(seconds=step_seconds * counter["n"])).isoformat(
            timespec="seconds"
        )

    with patch("dayplan.store._now", side_effect=_next):
        yield


def make_cfg(db_path: Path) -> Config:
    return Config(
        db_path=db_path,
        ticktick_token="test-token",
        ticktick_token_source="TICKTICK_TOKEN",
        asana_token=None,
        asana_workspaces=(),
        asana_projects=(),
        asana_sections=(),
        asana_only_mine=True,
        asana_include_subtasks=True,
        ticktick_due_within_days=None,
        ticktick_include_undated=True,
        jira_base_url=None,
        jira_site_url=None,
        jira_email=None,
        jira_api_token=None,
        jira_jql="",
        sync_interval_minutes=0,
        toggl_project_map=db_path.parent / "toggl-projects.json",
    )


def remote(external_id: str, title: str | None = None, **extra) -> RemoteTask:
    return RemoteTask(
        source="ticktick",
        external_id=external_id,
        title=title or f"Task {external_id}",
        **extra,
    )


class TempDbCase:
    """Mixin: a fresh sqlite file per test, a Config pointed at it, and a
    helper to run store.sync against a canned provider response with no
    network involved."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dayplan-test-"))
        self.cfg = make_cfg(self.tmp / "dayplan.sqlite")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def sync_with(self, tasks: list[RemoteTask], *, errors: dict | None = None):
        from dayplan import store

        with patch(
            "dayplan.store.fetch_all",
            return_value=({"ticktick": tasks}, errors or {}),
        ):
            return store.sync(self.cfg, ["ticktick"], trigger="cli")
