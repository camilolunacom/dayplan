"""All reads and writes that are not provider calls."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .dates import days_from_today, today_str
from .db import connect, set_meta
from .providers import SOURCES, RemoteTask, fetch_all
from .toggl import load_rules as load_toggl_rules
from .toggl import resolve as resolve_toggl

# One list, ordered by hand. `plan.day` keeps this sentinel for every row: the
# per-day buckets are gone, the list itself is the priority order.
LIST = "list"
BACKLOG = "backlog"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- sync


@dataclass
class SyncReport:
    added: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    closed: dict[str, int] = field(default_factory=dict)
    reopened: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "updated": self.updated,
            "closed": self.closed,
            "reopened": self.reopened,
            "errors": self.errors,
            "skipped": self.skipped,
        }

    @property
    def total_added(self) -> int:
        return sum(self.added.values())


def _next_ref(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(ref), 0) + 1 AS next FROM tasks").fetchone()
    return int(row["next"])


def _upsert(conn: sqlite3.Connection, task: RemoteTask, report: SyncReport) -> None:
    existing = conn.execute(
        "SELECT id, closed FROM tasks WHERE id = ?", (task.id,)
    ).fetchone()
    now = _now()
    payload = (
        task.title,
        task.url,
        task.project,
        task.status,
        task.priority,
        task.due,
        json.dumps(task.tags, ensure_ascii=False),
        task.notes,
        json.dumps(task.raw, ensure_ascii=False, default=str),
        task.toggl_project_id,
        now,
    )
    if existing is None:
        conn.execute(
            "INSERT INTO tasks(id, ref, source, external_id, title, url, project, status, "
            "priority, due, tags, notes, raw, toggl_project_id, first_seen, last_synced, "
            "closed) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (task.id, _next_ref(conn), task.source, task.external_id, *payload[:-1], now, now),
        )
        report.added[task.source] = report.added.get(task.source, 0) + 1
        return

    conn.execute(
        "UPDATE tasks SET title = ?, url = ?, project = ?, status = ?, priority = ?, due = ?, "
        "tags = ?, notes = ?, raw = ?, toggl_project_id = ?, last_synced = ?, closed = 0, "
        "closed_at = NULL WHERE id = ?",
        (*payload, task.id),
    )
    report.updated[task.source] = report.updated.get(task.source, 0) + 1
    if existing["closed"]:
        report.reopened[task.source] = report.reopened.get(task.source, 0) + 1


def _record_attempt(
    conn: sqlite3.Connection,
    source: str,
    started: str,
    *,
    ok: bool,
    fetched: int = 0,
    added: int = 0,
    updated: int = 0,
    closed: int = 0,
    error: str | None = None,
    trigger: str = "manual",
) -> None:
    conn.execute(
        "INSERT INTO sync_log(source, started_at, finished_at, ok, fetched, added, "
        "updated, closed, error, trigger) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, started, _now(), 1 if ok else 0, fetched, added, updated, closed, error, trigger),
    )


def sync(
    cfg: Config, sources: list[str] | None = None, trigger: str = "manual"
) -> SyncReport:
    """Pull from every configured (or requested) source and reconcile the cache."""
    report = SyncReport()
    started = _now()
    requested = sources or cfg.enabled_sources()
    if not requested:
        report.errors["config"] = "No source is configured. Run `dayplan doctor`."
        return report

    enabled = set(cfg.enabled_sources())
    targets = []
    for source in requested:
        if source in enabled:
            targets.append(source)
        else:
            report.skipped.append(source)

    fetched, errors = fetch_all(cfg, targets)
    report.errors.update(errors)

    # Resolve the Toggl project once per task here, where the Jira epic and the
    # Asana project are still available, instead of teaching the browser about
    # providers.
    rules = load_toggl_rules(cfg.toggl_project_map)
    for tasks in fetched.values():
        for task in tasks:
            task.toggl_project_id = resolve_toggl(task, rules)

    conn = connect(cfg.db_path)
    try:
        for source, message in errors.items():
            _record_attempt(conn, source, started, ok=False, error=message, trigger=trigger)

        for source, tasks in fetched.items():
            seen = set()
            for task in tasks:
                _upsert(conn, task, report)
                seen.add(task.id)

            # Anything we had open for this source and did not see is done or gone.
            rows = conn.execute(
                "SELECT id FROM tasks WHERE source = ? AND closed = 0", (source,)
            ).fetchall()
            gone = [row["id"] for row in rows if row["id"] not in seen]
            if gone:
                conn.executemany(
                    "UPDATE tasks SET closed = 1, closed_at = ? WHERE id = ?",
                    [(_now(), task_id) for task_id in gone],
                )
                report.closed[source] = len(gone)
            set_meta(conn, f"last_sync:{source}", _now())
            _record_attempt(
                conn,
                source,
                started,
                ok=True,
                fetched=len(tasks),
                added=report.added.get(source, 0),
                updated=report.updated.get(source, 0),
                closed=report.closed.get(source, 0),
                trigger=trigger,
            )
        set_meta(conn, "last_sync", _now())
        conn.commit()
    finally:
        conn.close()
    return report


# --------------------------------------------------------------------------- reads

TASK_SELECT = """
SELECT t.id, t.ref, t.source, t.external_id, t.title, t.url, t.project, t.status,
       t.priority, t.due, t.tags, t.notes, t.toggl_project_id, t.closed, t.closed_at, t.first_seen,
       p.day AS plan_day, p.position AS plan_position, p.note AS plan_note
FROM tasks t
LEFT JOIN plan p ON p.task_id = t.id
"""


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    due = row["due"]
    return {
        "id": row["id"],
        "ref": row["ref"],
        "source": row["source"],
        "external_id": row["external_id"],
        "title": row["title"],
        "url": row["url"],
        "project": row["project"],
        "status": row["status"],
        "priority": row["priority"],
        "due": due,
        "due_in_days": days_from_today(due),
        "overdue": bool(due and (days_from_today(due) or 0) < 0),
        "tags": json.loads(row["tags"] or "[]"),
        "notes": row["notes"],
        "toggl_project_id": row["toggl_project_id"],
        "closed": bool(row["closed"]),
        "closed_at": row["closed_at"],
        "first_seen": row["first_seen"],
        "day": row["plan_day"],
        "position": row["plan_position"],
        "plan_note": row["plan_note"],
        # Three zones, all derived from the plan row:
        #   ordered    a manual position -> he arranged it
        #   unordered  a row but no position -> he has seen it, not ranked it
        #   new        no row at all -> arrived from a sync, untriaged
        "zone": (
            "new"
            if row["plan_day"] is None
            else ("ordered" if row["plan_position"] is not None else "unordered")
        ),
    }


def _sort_key(task: dict[str, Any]) -> tuple:
    """Default ordering for anything not yet placed by hand.

    Overdue first, then by due date, then priority, then title. This only
    decides the tail of the list; the head is whatever he dragged.
    """
    due_in = task["due_in_days"]
    has_due = 0 if due_in is not None else 1
    return (has_due, due_in if due_in is not None else 0, -task["priority"], task["title"].lower())


def ordered_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The main list: hand-ordered items first, then the ones he has seen.

    Tasks that arrived from a sync and have never been touched are *not* here
    — they live in `new_tasks` so a sync cannot disturb an arrangement.
    """
    tasks = [t for t in list_tasks(conn) if t["zone"] != "new"]
    placed = [t for t in tasks if t["position"] is not None]
    seen = [t for t in tasks if t["position"] is None]
    placed.sort(key=lambda t: t["position"])
    # Stable on purpose: the seen-but-unranked tail must not reshuffle itself
    # every time a due date rolls over.
    seen.sort(key=lambda t: (t["first_seen"] or "", t["title"].lower()))
    pinned_ids = {t["id"] for t in placed}
    result = placed + seen
    for index, task in enumerate(result):
        task["rank"] = index
        task["pinned"] = task["id"] in pinned_ids
    return result


def new_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Untriaged arrivals, newest first. Kept out of the main list entirely."""
    rows = [t for t in list_tasks(conn) if t["zone"] == "new"]
    rows.sort(key=lambda t: (t["first_seen"] or "", t["title"].lower()), reverse=True)
    for task in rows:
        task["pinned"] = False
    return rows


def acknowledge(conn: sqlite3.Connection, task_id: str) -> None:
    """Move a new task into the main list without giving it a rank."""
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise ResolveError(f"unknown task id {task_id!r}")
    conn.execute(
        "INSERT INTO plan(task_id, day, position, updated_at) VALUES(?, ?, NULL, ?) "
        "ON CONFLICT(task_id) DO NOTHING",
        (task_id, LIST, _now()),
    )
    conn.commit()


def unacknowledge(conn: sqlite3.Connection, task_id: str) -> None:
    """Send a task back to the new pile.

    This drops the plan row, so the note goes with it — that is the point:
    the task returns to being untriaged.
    """
    row = conn.execute("SELECT day FROM plan WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        return
    conn.execute("DELETE FROM plan WHERE task_id = ?", (task_id,))
    _rewrite(conn, row["day"], _positions(conn, row["day"]))
    conn.commit()


def list_tasks(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    day: str | None = None,
    unplanned: bool = False,
    include_closed: bool = False,
    query: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if not include_closed:
        clauses.append("t.closed = 0")
    if source:
        clauses.append("t.source = ?")
        params.append(source)
    if day:
        clauses.append("p.day = ?")
        params.append(day)
    if unplanned:
        clauses.append("(p.day IS NULL OR p.day = ?)")
        params.append(BACKLOG)
    if query:
        clauses.append("(LOWER(t.title) LIKE ? OR LOWER(COALESCE(t.project, '')) LIKE ?)")
        needle = f"%{query.lower()}%"
        params.extend([needle, needle])

    sql = TASK_SELECT
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    tasks = [_row_to_task(row) for row in conn.execute(sql, params).fetchall()]

    if day:
        tasks.sort(key=lambda t: (t["position"] if t["position"] is not None else 1 << 30))
    else:
        tasks.sort(key=_sort_key)
    return tasks


def get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = conn.execute(TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


class ResolveError(ValueError):
    """A task reference matched nothing, or more than one thing."""


def resolve(conn: sqlite3.Connection, token: str) -> str:
    """Turn a user-supplied reference into a task id.

    Accepts a full id (jira:TN-1171), a short ref (#12 or 12), a Jira key
    (TN-1171), or a unique case-insensitive substring of the title.
    """
    token = token.strip()
    if not token:
        raise ResolveError("empty task reference")

    row = conn.execute("SELECT id FROM tasks WHERE id = ?", (token,)).fetchone()
    if row:
        return row["id"]

    bare = token.lstrip("#")
    if bare.isdigit():
        row = conn.execute("SELECT id FROM tasks WHERE ref = ?", (int(bare),)).fetchone()
        if row:
            return row["id"]
        raise ResolveError(f"no task with ref #{bare}")

    rows = conn.execute(
        "SELECT id, title FROM tasks WHERE LOWER(external_id) = ?", (token.lower(),)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]

    rows = conn.execute(
        "SELECT id, title FROM tasks WHERE closed = 0 AND LOWER(title) LIKE ? LIMIT 10",
        (f"%{token.lower()}%",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if not rows:
        raise ResolveError(f"nothing matches {token!r}")
    listing = "\n".join(f"  {r['id']}  {r['title']}" for r in rows)
    raise ResolveError(f"{token!r} is ambiguous, {len(rows)} matches:\n{listing}")


# --------------------------------------------------------------------------- plan writes


def _positions(conn: sqlite3.Connection, day: str) -> list[str]:
    """Task ids that have a manual position, in order. NULL positions are not
    part of the ordering and must not be renumbered into it."""
    rows = conn.execute(
        "SELECT task_id FROM plan WHERE day = ? AND position IS NOT NULL ORDER BY position",
        (day,),
    ).fetchall()
    return [row["task_id"] for row in rows]


def _rewrite(conn: sqlite3.Connection, day: str, ids: list[str]) -> None:
    now = _now()
    conn.executemany(
        "UPDATE plan SET position = ?, day = ?, updated_at = ? WHERE task_id = ?",
        [(index, day, now, task_id) for index, task_id in enumerate(ids)],
    )


def assign(
    conn: sqlite3.Connection, task_id: str, day: str, position: int | None = None
) -> dict[str, Any]:
    """Put a task on a day. position is a 0-based index; None appends."""
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise ResolveError(f"unknown task id {task_id!r}")

    current = conn.execute("SELECT day FROM plan WHERE task_id = ?", (task_id,)).fetchone()
    old_day = current["day"] if current else None

    order = [t for t in _positions(conn, day) if t != task_id]
    index = len(order) if position is None else max(0, min(int(position), len(order)))
    order.insert(index, task_id)

    now = _now()
    conn.execute(
        "INSERT INTO plan(task_id, day, position, updated_at) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(task_id) DO UPDATE SET day = excluded.day, position = excluded.position, "
        "updated_at = excluded.updated_at",
        (task_id, day, index, now),
    )
    _rewrite(conn, day, order)
    if old_day and old_day != day:
        _rewrite(conn, old_day, _positions(conn, old_day))
    conn.commit()
    return {"task_id": task_id, "day": day, "position": index}


def unassign(conn: sqlite3.Connection, task_id: str) -> None:
    """Clear the manual position so the task falls back to the default order.

    The note is kept: unpinning is a statement about ordering, not a reason
    to throw away what he wrote.
    """
    row = conn.execute("SELECT day FROM plan WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        return
    conn.execute(
        "UPDATE plan SET position = NULL, updated_at = ? WHERE task_id = ?", (_now(), task_id)
    )
    _rewrite(conn, row["day"], _positions(conn, row["day"]))
    conn.commit()


def set_order(conn: sqlite3.Connection, day: str, ids: list[str]) -> list[str]:
    """Set the exact order of a day. Ids not yet on that day are moved onto it.

    Tasks already on the day but missing from `ids` keep their relative order
    and are appended after, so a partial reorder never silently drops anything.
    """
    known = []
    for task_id in ids:
        if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
            raise ResolveError(f"unknown task id {task_id!r}")
        if task_id not in known:
            known.append(task_id)

    leftovers = [t for t in _positions(conn, day) if t not in known]
    final = known + leftovers

    now = _now()
    moved_from: set[str] = set()
    for task_id in known:
        row = conn.execute("SELECT day FROM plan WHERE task_id = ?", (task_id,)).fetchone()
        if row and row["day"] != day:
            moved_from.add(row["day"])
    conn.executemany(
        "INSERT INTO plan(task_id, day, position, updated_at) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(task_id) DO UPDATE SET day = excluded.day, position = excluded.position, "
        "updated_at = excluded.updated_at",
        [(task_id, day, index, now) for index, task_id in enumerate(final)],
    )
    for other in moved_from:
        _rewrite(conn, other, _positions(conn, other))
    conn.commit()
    return final


def set_list_order(conn: sqlite3.Connection, ids: list[str]) -> list[str]:
    """Pin these ids to the head of the single list, in this order."""
    return set_order(conn, LIST, ids)


def current_task(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The featured task: first in the list that is not ticked off."""
    return next_task(ordered_tasks(conn))


def update_plan(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: str | None = None,
    day: str | None = None,
) -> dict[str, Any] | None:
    """Update the local-only fields.

    Creates the plan row with no position if there is none, so annotating a
    task never changes where it sits in the list.
    """
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise ResolveError(f"unknown task id {task_id!r}")
    conn.execute(
        "INSERT INTO plan(task_id, day, position, updated_at) VALUES(?, ?, NULL, ?) "
        "ON CONFLICT(task_id) DO NOTHING",
        (task_id, day or LIST, _now()),
    )

    sets: list[str] = []
    params: list[Any] = []
    if note is not None:
        sets.append("note = ?")
        params.append(note or None)
    if sets:
        sets.append("updated_at = ?")
        params.append(_now())
        conn.execute(f"UPDATE plan SET {', '.join(sets)} WHERE task_id = ?", (*params, task_id))
    conn.commit()
    return get_task(conn, task_id)


# --------------------------------------------------------------------------- summary


def next_task(plan: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one to work on now: simply the first, since the order is the point.

    There is no local "done" any more -- a task leaves by being closed at the
    source, which the next sync notices.
    """
    return plan[0] if plan else None


# ------------------------------------------------------------------- integrations


def _why_not_configured(source: str, cfg: Config) -> str:
    return {
        "ticktick": "TICKTICK_TOKEN is not set",
        "asana": "ASANA_TOKEN is not set",
        "jira": "needs JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN",
    }.get(source, "not configured")


def integrations(conn: sqlite3.Connection, cfg: Config, history: int = 5) -> list[dict[str, Any]]:
    """Per-source health, for the dashboard status strip and `dayplan status`.

    Distinguishes four states, because "worked but returned nothing" is the
    failure mode that looks like success and costs the most time:

      off      not configured at all
      error    the last attempt failed; `error` says why
      empty    the last attempt succeeded but the provider returned 0 tasks
      ok       synced and holding tasks
    """
    enabled = set(cfg.enabled_sources())
    result = []
    for source in SOURCES:
        open_count = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE source = ? AND closed = 0", (source,)
        ).fetchone()["n"]

        rows = conn.execute(
            "SELECT started_at, finished_at, ok, fetched, added, updated, closed, error, "
            "trigger FROM sync_log WHERE source = ? ORDER BY finished_at DESC, id DESC LIMIT ?",
            (source, history),
        ).fetchall()
        attempts = [dict(row) for row in rows]
        for attempt in attempts:
            attempt["ok"] = bool(attempt["ok"])
        last = attempts[0] if attempts else None

        last_success = conn.execute(
            "SELECT finished_at FROM sync_log WHERE source = ? AND ok = 1 "
            "ORDER BY finished_at DESC, id DESC LIMIT 1",
            (source,),
        ).fetchone()

        configured = source in enabled
        if not configured:
            state = "off"
        elif last is None:
            state = "never"
        elif not last["ok"]:
            state = "error"
        elif last["fetched"] == 0:
            state = "empty"
        else:
            state = "ok"

        result.append(
            {
                "source": source,
                "configured": configured,
                "state": state,
                "detail": None if configured else _why_not_configured(source, cfg),
                "open_count": open_count,
                "last_attempt_at": last["finished_at"] if last else None,
                "last_success_at": last_success["finished_at"] if last_success else None,
                "last_error": last["error"] if last and not last["ok"] else None,
                "last_fetched": last["fetched"] if last else None,
                "history": attempts,
            }
        )
    return result


def sync_log(conn: sqlite3.Connection, limit: int = 30, source: str | None = None) -> list[dict[str, Any]]:
    """Raw recent attempts, newest first."""
    if source:
        rows = conn.execute(
            "SELECT * FROM sync_log WHERE source = ? ORDER BY finished_at DESC, id DESC LIMIT ?",
            (source, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sync_log ORDER BY finished_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["ok"] = bool(item["ok"])
        out.append(item)
    return out


def summary(conn: sqlite3.Connection, day: str | None = None) -> dict[str, Any]:
    day = day or today_str()
    tasks = ordered_tasks(conn)
    fresh = new_tasks(conn)

    by_source: dict[str, int] = {}
    for task in tasks + fresh:
        by_source[task["source"]] = by_source.get(task["source"], 0) + 1

    overdue = [t for t in tasks + fresh if t["overdue"]]
    due_today = [t for t in tasks + fresh if t["due_in_days"] == 0]

    last_sync = {}
    for row in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'last_sync:%'").fetchall():
        last_sync[row["key"].split(":", 1)[1]] = row["value"]

    return {
        "day": day,
        "current": next_task(tasks),
        "tasks": tasks,
        "count": len(tasks),
        "pinned_count": len([t for t in tasks if t["pinned"]]),
        "new_count": len(fresh),
        "new": fresh[:20],
        "by_source": by_source,
        "overdue_count": len(overdue),
        "overdue": overdue[:20],
        "due_today_count": len(due_today),
        "last_sync": last_sync,
    }
