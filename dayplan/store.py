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
from .providers import RemoteTask, fetch_all

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
        now,
    )
    if existing is None:
        conn.execute(
            "INSERT INTO tasks(id, ref, source, external_id, title, url, project, status, "
            "priority, due, tags, notes, raw, first_seen, last_synced, closed) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (task.id, _next_ref(conn), task.source, task.external_id, *payload[:-1], now, now),
        )
        report.added[task.source] = report.added.get(task.source, 0) + 1
        return

    conn.execute(
        "UPDATE tasks SET title = ?, url = ?, project = ?, status = ?, priority = ?, due = ?, "
        "tags = ?, notes = ?, raw = ?, last_synced = ?, closed = 0, closed_at = NULL WHERE id = ?",
        (*payload, task.id),
    )
    report.updated[task.source] = report.updated.get(task.source, 0) + 1
    if existing["closed"]:
        report.reopened[task.source] = report.reopened.get(task.source, 0) + 1


def sync(cfg: Config, sources: list[str] | None = None) -> SyncReport:
    """Pull from every configured (or requested) source and reconcile the cache."""
    report = SyncReport()
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

    conn = connect(cfg.db_path)
    try:
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
        set_meta(conn, "last_sync", _now())
        conn.commit()
    finally:
        conn.close()
    return report


# --------------------------------------------------------------------------- reads

TASK_SELECT = """
SELECT t.id, t.ref, t.source, t.external_id, t.title, t.url, t.project, t.status,
       t.priority, t.due, t.tags, t.notes, t.closed, t.closed_at, t.first_seen,
       p.day AS plan_day, p.position AS plan_position, p.note AS plan_note,
       p.est_minutes, p.done_local
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
        "closed": bool(row["closed"]),
        "closed_at": row["closed_at"],
        "day": row["plan_day"],
        "position": row["plan_position"],
        "plan_note": row["plan_note"],
        "est_minutes": row["est_minutes"],
        "done": bool(row["done_local"]),
    }


def _sort_key(task: dict[str, Any]) -> tuple:
    """Backlog ordering: overdue first, then by due date, then priority, then title."""
    due_in = task["due_in_days"]
    has_due = 0 if due_in is not None else 1
    return (has_due, due_in if due_in is not None else 0, -task["priority"], task["title"].lower())


def list_tasks(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    day: str | None = None,
    unplanned: bool = False,
    include_closed: bool = False,
    include_done: bool = True,
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
    if not include_done:
        tasks = [t for t in tasks if not t["done"]]

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
    rows = conn.execute(
        "SELECT task_id FROM plan WHERE day = ? ORDER BY position", (day,)
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
    """Drop a task off the calendar entirely (back to the pending pool)."""
    row = conn.execute("SELECT day FROM plan WHERE task_id = ?", (task_id,)).fetchone()
    conn.execute("DELETE FROM plan WHERE task_id = ?", (task_id,))
    if row:
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


def update_plan(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: str | None = None,
    est_minutes: int | None = None,
    done: bool | None = None,
    day: str | None = None,
) -> dict[str, Any] | None:
    """Update the local-only fields. Creates the plan row if needed."""
    if not conn.execute("SELECT 1 FROM plan WHERE task_id = ?", (task_id,)).fetchone():
        assign(conn, task_id, day or today_str())

    sets: list[str] = []
    params: list[Any] = []
    if note is not None:
        sets.append("note = ?")
        params.append(note or None)
    if est_minutes is not None:
        sets.append("est_minutes = ?")
        params.append(est_minutes if est_minutes > 0 else None)
    if done is not None:
        sets.append("done_local = ?")
        params.append(1 if done else 0)
    if sets:
        sets.append("updated_at = ?")
        params.append(_now())
        conn.execute(f"UPDATE plan SET {', '.join(sets)} WHERE task_id = ?", (*params, task_id))
    if day is not None:
        assign(conn, task_id, day)
    conn.commit()
    return get_task(conn, task_id)


# --------------------------------------------------------------------------- summary


def summary(conn: sqlite3.Connection, day: str | None = None) -> dict[str, Any]:
    day = day or today_str()
    plan = list_tasks(conn, day=day)
    pending = list_tasks(conn, unplanned=True)

    by_source: dict[str, int] = {}
    for task in pending:
        by_source[task["source"]] = by_source.get(task["source"], 0) + 1

    overdue = [t for t in pending if t["overdue"]]
    due_today = [t for t in pending if t["due_in_days"] == 0]
    estimated = sum(t["est_minutes"] or 0 for t in plan if not t["done"])

    last_sync = {}
    for row in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'last_sync:%'").fetchall():
        last_sync[row["key"].split(":", 1)[1]] = row["value"]

    return {
        "day": day,
        "plan": plan,
        "plan_count": len(plan),
        "plan_open": len([t for t in plan if not t["done"]]),
        "plan_done": len([t for t in plan if t["done"]]),
        "plan_estimated_minutes": estimated,
        "pending_count": len(pending),
        "pending_by_source": by_source,
        "overdue_count": len(overdue),
        "overdue": overdue[:20],
        "due_today_count": len(due_today),
        "last_sync": last_sync,
    }
