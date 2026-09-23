"""All reads and writes that are not provider calls."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import Config
from .dates import days_from_today
from .db import connect, get_meta, set_meta
from .providers import SOURCES, RemoteTask, fetch_all
from .toggl import load_rules as load_toggl_rules
from .toggl import resolve as resolve_toggl

# One list, ordered by hand: the list itself is the priority order.


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


class ConflictError(RuntimeError):
    """The order (or other plan state) moved under the caller between their
    read and their write."""


def get_order_revision(conn: sqlite3.Connection) -> int:
    """A counter bumped on every mutation that changes plan membership/order
    or visible list membership -- what a client compares against to detect a
    stale write. See `_bump_order_revision`."""
    return int(get_meta(conn, "order_revision") or "0")


def _bump_order_revision(conn: sqlite3.Connection) -> int:
    new = get_order_revision(conn) + 1
    set_meta(conn, "order_revision", str(new))
    return new


# A departure is confirmed only after this many consecutive successful,
# non-empty syncs from a source omit the task while returning at least one
# other task from that source. One partial miss is noise (a provider paging
# hiccup, a filter that briefly excluded it); two in a row is a pattern.
ABSENCE_CONFIRM_THRESHOLD = 2


def _upsert(conn: sqlite3.Connection, task: RemoteTask, report: SyncReport) -> None:
    existing = conn.execute(
        "SELECT id, closed, needs_retriage FROM tasks WHERE id = ?", (task.id,)
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
        task.toggl_project,
        task.toggl_project_id,
        now,
    )
    if existing is None:
        conn.execute(
            "INSERT INTO tasks(id, ref, source, external_id, title, url, project, status, "
            "priority, due, tags, notes, raw, toggl_project, toggl_project_id, first_seen, "
            "last_synced, closed, absence_streak, needs_retriage) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0)",
            (task.id, _next_ref(conn), task.source, task.external_id, *payload[:-1], now, now),
        )
        report.added[task.source] = report.added.get(task.source, 0) + 1
        return

    # Being seen resets the absence streak regardless of anything else: a
    # task that was flaking in and out of a provider's feed is not "departing"
    # once it shows back up.
    reopening = bool(existing["needs_retriage"])
    conn.execute(
        "UPDATE tasks SET title = ?, url = ?, project = ?, status = ?, priority = ?, due = ?, "
        "tags = ?, notes = ?, raw = ?, toggl_project = ?, toggl_project_id = ?, "
        "last_synced = ?, closed = 0, closed_at = NULL, absence_streak = 0, "
        "needs_retriage = 0" + (", reappeared_at = ?" if reopening else "") + " WHERE id = ?",
        (*payload, now, task.id) if reopening else (*payload, task.id),
    )
    report.updated[task.source] = report.updated.get(task.source, 0) + 1
    if existing["closed"]:
        report.reopened[task.source] = report.reopened.get(task.source, 0) + 1

    if reopening:
        # Confirmed-departed, and now back: the old plan row (and whatever
        # manual position it carried) belongs to the task that left. Drop it
        # atomically with clearing the retriage flag so it lands back in the
        # New pile, sorted as a fresh arrival by `reappeared_at`, not by its
        # original `first_seen`.
        conn.execute("DELETE FROM plan WHERE task_id = ?", (task.id,))
        _bump_order_revision(conn)


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


def _sync_lock_path(cfg: Config) -> Path:
    return cfg.db_path.parent / f"{cfg.db_path.name}.sync.lock"


@contextlib.contextmanager
def _sync_lock(cfg: Config) -> Iterator[None]:
    """Serialize the whole sync -- provider fetch through DB commit -- across
    threads and separate processes (scheduled loop, `/api/sync`, the CLI)
    that share this database path.

    A scheduled sync, a manual API sync and a CLI sync can all be triggered
    independently, and without this, their fetch-then-apply steps could
    interleave: a later-started sync could confirm a departure and commit,
    then an earlier-started one could resume with its now-stale snapshot and
    spuriously reopen or delete plan state. `flock` on a file next to the
    database serializes them regardless of process boundary; opening an
    independent fd per call means the lock is released by close (or by the
    process dying) without needing any cleanup handshake.
    """
    lock_path = _sync_lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


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

    with _sync_lock(cfg):
        return _sync_locked(cfg, requested, report, started, trigger)


def _sync_locked(
    cfg: Config,
    requested: list[str],
    report: SyncReport,
    started: str,
    trigger: str,
) -> SyncReport:
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
            task.toggl_project, task.toggl_project_id = resolve_toggl(task, rules)

    conn = connect(cfg.db_path)
    try:
        for source, message in errors.items():
            _record_attempt(conn, source, started, ok=False, error=message, trigger=trigger)

        for source, tasks in fetched.items():
            seen = set()
            for task in tasks:
                _upsert(conn, task, report)
                seen.add(task.id)

            # A successful-but-empty response says nothing about which tasks
            # left: it is far more likely a provider hiccup (rate limit, an
            # empty page) than every open task vanishing at once, so it must
            # not advance anyone's absence streak, let alone close anything.
            if tasks:
                rows = conn.execute(
                    "SELECT id, absence_streak FROM tasks WHERE source = ? AND closed = 0",
                    (source,),
                ).fetchall()
                absent = [row for row in rows if row["id"] not in seen]
                newly_closed = 0
                for row in absent:
                    streak = row["absence_streak"] + 1
                    if streak >= ABSENCE_CONFIRM_THRESHOLD:
                        # Confirmed departure: close it, but keep its plan row
                        # exactly as it is -- a transient empty/partial
                        # response must never destroy a manual position, and
                        # the row is what lets a later return be recognized as
                        # *this* task's reopening rather than a fresh add.
                        conn.execute(
                            "UPDATE tasks SET closed = 1, closed_at = ?, needs_retriage = 1, "
                            "absence_streak = ? WHERE id = ?",
                            (_now(), streak, row["id"]),
                        )
                        newly_closed += 1
                    else:
                        conn.execute(
                            "UPDATE tasks SET absence_streak = ? WHERE id = ?",
                            (streak, row["id"]),
                        )
                if newly_closed:
                    report.closed[source] = newly_closed
                    _bump_order_revision(conn)
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
       t.priority, t.due, t.tags, t.notes, t.toggl_project, t.toggl_project_id,
       t.closed, t.closed_at, t.first_seen, t.reappeared_at,
       p.task_id AS plan_row, p.position AS plan_position
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
        "toggl_project": row["toggl_project"],
        "toggl_project_id": row["toggl_project_id"],
        "closed": bool(row["closed"]),
        "closed_at": row["closed_at"],
        "first_seen": row["first_seen"],
        "reappeared_at": row["reappeared_at"],
        "position": row["plan_position"],
        # Three zones, all derived from the plan row:
        #   ordered    a manual position -> he arranged it
        #   unordered  a row but no position -> he has seen it, not ranked it
        #   new        no row at all -> arrived from a sync, untriaged
        "zone": (
            "new"
            if row["plan_row"] is None
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


def _arrival_key(task: dict[str, Any]) -> str:
    """When a task counts as having arrived in the New pile.

    A task reopening after a confirmed departure gets `reappeared_at` set,
    which must win over its original `first_seen` -- otherwise it would sort
    by ancient history instead of showing up as the fresh arrival it is.
    """
    return task["reappeared_at"] or task["first_seen"] or ""


def new_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Untriaged arrivals, newest first. Kept out of the main list entirely."""
    rows = [t for t in list_tasks(conn) if t["zone"] == "new"]
    rows.sort(key=lambda t: (_arrival_key(t), t["title"].lower()), reverse=True)
    for task in rows:
        task["pinned"] = False
    return rows


def acknowledge(conn: sqlite3.Connection, task_id: str) -> None:
    """Move a new task into the main list without giving it a rank."""
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise ResolveError(f"unknown task id {task_id!r}")
    cursor = conn.execute(
        "INSERT INTO plan(task_id, position, updated_at) VALUES(?, NULL, ?) "
        "ON CONFLICT(task_id) DO NOTHING",
        (task_id, _now()),
    )
    if cursor.rowcount:  # a no-op (already acknowledged) must not bump the revision
        _bump_order_revision(conn)
    conn.commit()


def unacknowledge(conn: sqlite3.Connection, task_id: str) -> None:
    """Send a task back to the new pile.

    This drops the plan row, so it loses its place in the order — that is the
    point: the task returns to being untriaged.
    """
    if not conn.execute("SELECT 1 FROM plan WHERE task_id = ?", (task_id,)).fetchone():
        return
    conn.execute("DELETE FROM plan WHERE task_id = ?", (task_id,))
    _renumber(conn, _positions(conn))
    _bump_order_revision(conn)
    conn.commit()


def list_tasks(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
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
    if query:
        clauses.append("(LOWER(t.title) LIKE ? OR LOWER(COALESCE(t.project, '')) LIKE ?)")
        needle = f"%{query.lower()}%"
        params.extend([needle, needle])

    sql = TASK_SELECT
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    tasks = [_row_to_task(row) for row in conn.execute(sql, params).fetchall()]
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


def _positions(conn: sqlite3.Connection) -> list[str]:
    """Task ids that carry a manual position, in that order.

    Rows with position NULL are in the list but unranked, so they are not part
    of the ordering and must never be renumbered into it.
    """
    rows = conn.execute(
        "SELECT task_id FROM plan WHERE position IS NOT NULL ORDER BY position"
    ).fetchall()
    return [row["task_id"] for row in rows]


def _renumber(conn: sqlite3.Connection, ids: list[str]) -> None:
    """Write 0..n-1 across exactly these ids, in this order."""
    now = _now()
    conn.executemany(
        "UPDATE plan SET position = ?, updated_at = ? WHERE task_id = ?",
        [(index, now, task_id) for index, task_id in enumerate(ids)],
    )


UPSERT_POSITION = (
    "INSERT INTO plan(task_id, position, updated_at) VALUES(?, ?, ?) "
    "ON CONFLICT(task_id) DO UPDATE SET position = excluded.position, "
    "updated_at = excluded.updated_at"
)


def assign(
    conn: sqlite3.Connection, task_id: str, position: int | None = None
) -> dict[str, Any]:
    """Give a task a manual position. 0-based; None appends to the ranked head."""
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise ResolveError(f"unknown task id {task_id!r}")

    order = [t for t in _positions(conn) if t != task_id]
    index = len(order) if position is None else max(0, min(int(position), len(order)))
    order.insert(index, task_id)

    conn.execute(UPSERT_POSITION, (task_id, index, _now()))
    _renumber(conn, order)
    _bump_order_revision(conn)
    conn.commit()
    return {"task_id": task_id, "position": index}


def unassign(conn: sqlite3.Connection, task_id: str) -> None:
    """Clear the manual position so the task falls back to the default order.

    The row itself stays, so the task remains part of the list rather than
    falling back into the new pile.
    """
    row = conn.execute("SELECT position FROM plan WHERE task_id = ?", (task_id,)).fetchone()
    if row is None or row["position"] is None:  # no row, or already unranked: a no-op
        return
    conn.execute(
        "UPDATE plan SET position = NULL, updated_at = ? WHERE task_id = ?", (_now(), task_id)
    )
    _renumber(conn, _positions(conn))
    _bump_order_revision(conn)
    conn.commit()


def set_order(
    conn: sqlite3.Connection, ids: list[str], *, expected_revision: int | None = None
) -> list[str]:
    """Pin these ids as the ranked head of the list, in this order.

    Already-ranked tasks left out of `ids` keep their relative order and
    follow after, so a partial reorder never silently drops work.

    `expected_revision`, when given, is compared against the current order
    revision inside the same write transaction that performs the update, so a
    stale caller (a drag started against an order that has since changed --
    for instance a task confirmed-reopened out from under it) is rejected
    with `ConflictError` instead of silently clobbering what changed.
    """
    known: list[str] = []
    for task_id in ids:
        if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
            raise ResolveError(f"unknown task id {task_id!r}")
        if task_id not in known:
            known.append(task_id)

    conn.execute("BEGIN IMMEDIATE")
    try:
        if expected_revision is not None and get_order_revision(conn) != expected_revision:
            raise ConflictError(
                f"order revision {expected_revision} is stale; reload and try again"
            )

        leftovers = [t for t in _positions(conn) if t not in known]
        final = known + leftovers

        now = _now()
        conn.executemany(
            UPSERT_POSITION, [(task_id, index, now) for index, task_id in enumerate(final)]
        )
        _bump_order_revision(conn)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return final


def next_task(plan: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one to work on now: simply the first, since the order is the point.

    Nothing is completed from inside dayplan -- a task leaves by being closed
    at its source, which the next sync notices.
    """
    return plan[0] if plan else None


def current_task(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The featured task: the head of the list."""
    return next_task(ordered_tasks(conn))


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


def state_snapshot(conn: sqlite3.Connection, cfg: Config, *, history: int = 3) -> dict[str, Any]:
    """Every DB-derived `/api/state` field, read from one consistent point in
    time.

    Without an explicit transaction, each read here would autocommit on its
    own, and a concurrent sync or plan mutation could land in the gap between
    them -- pairing a stale task list with a revision that describes a state
    the list does not reflect. That silently defeats `/api/order`'s
    stale-write check, which trusts the revision to describe the list it came
    with. Wrapping every read in one transaction gives WAL snapshot isolation
    instead: whichever commits land before or after this transaction starts,
    every read inside it sees the same single point in time.
    """
    conn.execute("BEGIN")
    try:
        tasks = ordered_tasks(conn)
        fresh = new_tasks(conn)
        summ = summary(conn)
        integ = integrations(conn, cfg, history=history)
        revision = get_order_revision(conn)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return {
        "current": next_task(tasks) or next_task(fresh),
        "tasks": tasks,
        "new": fresh,
        "summary": summ,
        "integrations": integ,
        "order_revision": revision,
    }


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


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
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
