"""Provider contract: every provider returns a list of RemoteTask, read-only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config

SOURCES = ("ticktick", "asana", "jira")


class ProviderError(RuntimeError):
    """A provider could not be reached or refused the credentials."""


@dataclass
class RemoteTask:
    source: str
    external_id: str
    title: str
    url: str | None = None
    project: str | None = None
    status: str | None = None
    priority: int = 0  # 0 none, 1 low, 2 medium, 3 high
    due: str | None = None  # YYYY-MM-DD
    tags: list[str] = field(default_factory=list)
    notes: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.external_id}"


def fetch_source(source: str, cfg: Config) -> list[RemoteTask]:
    """Fetch open tasks from one source. Raises ProviderError if not usable."""
    if source == "ticktick":
        from .ticktick import fetch

        return fetch(cfg)
    if source == "asana":
        from .asana import fetch

        return fetch(cfg)
    if source == "jira":
        from .jira import fetch

        return fetch(cfg)
    raise ProviderError(f"unknown source: {source}")


def fetch_all(cfg: Config, sources: list[str] | None = None) -> tuple[dict[str, list[RemoteTask]], dict[str, str]]:
    """Fetch several sources. Returns (tasks_by_source, errors_by_source).

    A failing provider never takes down the others: its error is collected and
    its previously synced tasks are left untouched by the caller.
    """
    targets = sources if sources else cfg.enabled_sources()
    results: dict[str, list[RemoteTask]] = {}
    errors: dict[str, str] = {}
    for source in targets:
        try:
            results[source] = fetch_source(source, cfg)
        except ProviderError as exc:
            errors[source] = str(exc)
        except Exception as exc:  # noqa: BLE001 - never let one provider break sync
            errors[source] = f"{type(exc).__name__}: {exc}"
    return results, errors
