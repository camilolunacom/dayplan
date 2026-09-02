"""dayplan CLI. Talks straight to SQLite, so no server has to be running."""

from __future__ import annotations

import json as jsonlib
import sqlite3
import sys
from typing import Any

import typer

from . import store
from .config import load_config
from .db import connect, get_meta
from .toggl import load_rules as load_toggl_rules

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Pull tasks from TickTick, Asana and Jira; order them; plan the day.",
)

SOURCE_WIDTH = 8


def _conn() -> sqlite3.Connection:
    return connect(load_config().db_path)


def _echo_json(payload: Any) -> None:
    typer.echo(jsonlib.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _resolve_many(conn: sqlite3.Connection, refs: list[str]) -> list[str]:
    resolved = []
    for ref in refs:
        try:
            resolved.append(store.resolve(conn, ref))
        except store.ResolveError as exc:
            _fail(str(exc))
    return resolved


def _fmt_minutes(total: int) -> str:
    if not total:
        return "0m"
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _due_cell(task: dict[str, Any]) -> str:
    if not task["due"]:
        return " " * 12
    days = task["due_in_days"]
    if days is None:
        return f"{task['due']:<12}"
    if days < 0:
        return f"{task['due']}!"
    return f"{task['due']} "


def _priority_cell(priority: int) -> str:
    return {3: "!!!", 2: " !!", 1: "  !"}.get(priority, "   ")


def _task_line(task: dict[str, Any], prefix: str = "") -> str:
    est = f" ({_fmt_minutes(task['est_minutes'])})" if task["est_minutes"] else ""
    project = f" · {task['project']}" if task["project"] else ""
    note = f"\n{' ' * (len(prefix) + 6)}↳ {task['plan_note']}" if task["plan_note"] else ""
    return (
        f"{prefix}#{task['ref']:<4} {_due_cell(task)} {_priority_cell(task['priority'])} "
        f"{task['source']:<{SOURCE_WIDTH}} {task['title']}{project}{est}{note}"
    )


def _print_tasks(tasks: list[dict[str, Any]], numbered: bool = False) -> None:
    if not tasks:
        typer.echo("  (nothing)")
        return
    upcoming = store.next_task(tasks) if numbered else None
    # The divider only means something when part of the list is hand-ordered.
    has_pinned = any(t.get("pinned") for t in tasks)
    shown_divider = False
    for index, task in enumerate(tasks, start=1):
        if numbered and has_pinned and not shown_divider and not task.get("pinned"):
            typer.secho("     ── unordered below ──", fg=typer.colors.BRIGHT_BLACK)
            shown_divider = True
        if numbered:
            mark = "x" if task["done"] else " "
            # The task to work on now gets an arrow instead of its number.
            is_next = bool(upcoming and task["id"] == upcoming["id"])
            # Same width either way so the column stays aligned.
            slot = " ▶ " if is_next else f"{index:>2}."
            line = _task_line(task, prefix=f"{slot} [{mark}] ")
            if is_next:
                typer.secho(line, fg=typer.colors.CYAN, bold=True)
            else:
                typer.echo(line)
        else:
            typer.echo(_task_line(task, prefix="    "))


# --------------------------------------------------------------------------- commands


@app.command()
def doctor() -> None:
    """Show which sources are configured and where the data lives."""
    cfg = load_config()
    typer.echo(f"database        {cfg.db_path}")
    typer.echo(f"ticktick        {'ok' if cfg.ticktick_token else 'MISSING'}  ({cfg.ticktick_token_source})")
    window = (
        "all"
        if cfg.ticktick_due_within_days is None
        else f"due within {cfg.ticktick_due_within_days}d"
        + (" + undated" if cfg.ticktick_include_undated else ", undated excluded")
    )
    typer.echo(f"  filter        {window}")
    typer.echo(f"asana           {'ok' if cfg.asana_token else 'MISSING'}  (ASANA_TOKEN)")
    if cfg.asana_projects:
        typer.echo(f"  projects      {', '.join(cfg.asana_projects)} (workspaces ignored)")
        typer.echo(f"  sections      {', '.join(cfg.asana_sections) or 'all'}")
    else:
        typer.echo(f"  workspaces    {', '.join(cfg.asana_workspaces) or 'all visible'}")
    typer.echo(
        f"  filter        {'assigned to me' if cfg.asana_only_mine else 'anyone'}"
        f"{', + subtasks' if cfg.asana_include_subtasks else ', no subtasks'}"
    )
    typer.echo(f"jira            {'ok' if cfg.jira_ready else 'MISSING'}  ({cfg.jira_base_url or 'JIRA_BASE_URL unset'})")
    scoped = bool(cfg.jira_base_url and "api.atlassian.com" in cfg.jira_base_url)
    typer.echo(f"  token type    {'scoped (needs read:jira-work + read:jira-user)' if scoped else 'unscoped / site URL'}")
    typer.echo(f"  links via     {cfg.jira_site_url or 'resolved from /serverInfo at sync time'}")
    typer.echo(f"  jql           {cfg.jira_jql}")
    rules = load_toggl_rules(cfg.toggl_project_map)
    total = sum(len(v) for v in rules.values())
    typer.echo(f"toggl map       {cfg.toggl_project_map}")
    typer.echo(
        f"  rules         {total} across {', '.join(sorted(rules)) or 'nothing'}"
        f"{'' if total else '  (no mapping: every task tracks without a project)'}"
    )
    enabled = cfg.enabled_sources()
    typer.echo(f"enabled         {', '.join(enabled) if enabled else 'none'}")

    conn = _conn()
    try:
        for source in ("ticktick", "asana", "jira"):
            stamp = get_meta(conn, f"last_sync:{source}")
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE source = ? AND closed = 0", (source,)
            ).fetchone()["n"]
            typer.echo(f"  {source:<12} {count:>4} open   last sync {stamp or 'never'}")
    finally:
        conn.close()

    if not enabled:
        typer.echo("\nNothing is configured yet. See the README, then re-run `dayplan doctor`.")


@app.command()
def sync(
    source: list[str] = typer.Option(
        None, "--source", "-s", help="Limit to these sources (repeatable)."
    ),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Pull tasks from every configured source."""
    report = store.sync(load_config(), list(source) if source else None, trigger="cli")
    if json:
        _echo_json(report.as_dict())
        raise typer.Exit(code=1 if report.errors else 0)

    for name in ("added", "updated", "closed", "reopened"):
        data = getattr(report, name)
        if data:
            detail = "  ".join(f"{k} {v}" for k, v in sorted(data.items()))
            typer.echo(f"{name:<9} {detail}")
    if report.skipped:
        typer.secho(f"skipped   {', '.join(report.skipped)} (not configured)", fg=typer.colors.YELLOW)
    for name, message in report.errors.items():
        typer.secho(f"error     {name}: {message}", fg=typer.colors.RED)
    if not any([report.added, report.updated, report.closed, report.errors]):
        typer.echo("nothing changed")
    raise typer.Exit(code=1 if report.errors else 0)


@app.command("list")
def list_cmd(
    source: str = typer.Option(None, "--source", "-s"),
    query: str = typer.Option(None, "--query", "-q", help="Substring of title or project."),
    limit: int = typer.Option(0, "--limit", "-n"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """The list, in priority order. ▶ marks the one to work on now."""
    conn = _conn()
    try:
        tasks = store.ordered_tasks(conn)
    finally:
        conn.close()
    if source:
        tasks = [t for t in tasks if t["source"] == source]
    if query:
        needle = query.lower()
        tasks = [
            t
            for t in tasks
            if needle in t["title"].lower() or needle in (t["project"] or "").lower()
        ]
    if limit > 0:
        tasks = tasks[:limit]
    if json:
        _echo_json(tasks)
        return
    pinned = len([t for t in tasks if t["pinned"]])
    typer.echo(f"{len(tasks)} task(s), {pinned} ordered by hand")
    _print_tasks(tasks, numbered=True)


@app.command()
def show(ref: str, json: bool = typer.Option(False, "--json")) -> None:
    """Show one task in full."""
    conn = _conn()
    try:
        task_id = _resolve_many(conn, [ref])[0]
        task = store.get_task(conn, task_id)
    finally:
        conn.close()
    if not task:
        _fail(f"no task {ref}")
    if json:
        _echo_json(task)
        return
    for key, value in task.items():
        typer.echo(f"{key:<14} {value}")


@app.command()
def current(json: bool = typer.Option(False, "--json")) -> None:
    """The featured task: first in the list that is not done."""
    conn = _conn()
    try:
        task = store.current_task(conn)
    finally:
        conn.close()
    if json:
        _echo_json(task)
        return
    if not task:
        typer.echo("Nothing left in the list.")
        raise typer.Exit()
    typer.secho("Now:", fg=typer.colors.CYAN, bold=True)
    typer.echo(_task_line(task, prefix="  \u25b6 "))
    if task["url"]:
        typer.echo(f"    {task['url']}")


@app.command("next")
def next_cmd(json: bool = typer.Option(False, "--json")) -> None:
    """Alias of `current`."""
    current(json)


@app.command()
def order(refs: list[str] = typer.Argument(..., help="Refs in the order you want them.")) -> None:
    """Pin these tasks to the top of the list, in this order.

    Anything already pinned that you leave out keeps its relative order and
    follows after, so a partial reorder never drops work.
    """
    conn = _conn()
    try:
        store.set_list_order(conn, _resolve_many(conn, refs))
        tasks = store.ordered_tasks(conn)
    finally:
        conn.close()
    _print_tasks(tasks[: max(8, len(refs) + 3)], numbered=True)


@app.command()
def top(ref: str) -> None:
    """Make one task the current one, moving it to the head of the list."""
    conn = _conn()
    try:
        task_id = _resolve_many(conn, [ref])[0]
        pinned = [t["id"] for t in store.ordered_tasks(conn) if t["pinned"]]
        store.set_list_order(conn, [task_id] + [i for i in pinned if i != task_id])
        tasks = store.ordered_tasks(conn)
    finally:
        conn.close()
    _print_tasks(tasks[:6], numbered=True)


@app.command()
def unpin(refs: list[str] = typer.Argument(...)) -> None:
    """Drop a manual position; the task falls back to the default order."""
    conn = _conn()
    try:
        for task_id in _resolve_many(conn, refs):
            store.unassign(conn, task_id)
            typer.echo(f"{task_id} unpinned")
    finally:
        conn.close()


@app.command()
def note(ref: str, text: str = typer.Argument(..., help="Use '' to clear.")) -> None:
    """Attach a local planning note to a task."""
    conn = _conn()
    try:
        task_id = _resolve_many(conn, [ref])[0]
        store.update_plan(conn, task_id, note=text, day=store.LIST)
        typer.echo(f"{task_id} note set")
    finally:
        conn.close()


@app.command()
def est(ref: str, minutes: int = typer.Argument(..., help="0 clears the estimate.")) -> None:
    """Set a time estimate, in minutes."""
    conn = _conn()
    try:
        task_id = _resolve_many(conn, [ref])[0]
        store.update_plan(conn, task_id, est_minutes=minutes, day=store.LIST)
        typer.echo(f"{task_id} estimate {_fmt_minutes(minutes)}")
    finally:
        conn.close()


@app.command()
def done(
    refs: list[str] = typer.Argument(...),
    undo: bool = typer.Option(False, "--undo", help="Mark as not done instead."),
) -> None:
    """Tick a task off locally. v1 does not push this back to the source."""
    conn = _conn()
    try:
        for task_id in _resolve_many(conn, refs):
            store.update_plan(conn, task_id, done=not undo, day=store.LIST)
            typer.echo(f"{task_id} {'reopened' if undo else 'done (local only)'}")
    finally:
        conn.close()


STATE_LABEL = {
    "ok": ("ok", typer.colors.GREEN),
    "empty": ("EMPTY", typer.colors.YELLOW),
    "error": ("ERROR", typer.colors.RED),
    "never": ("never synced", typer.colors.YELLOW),
    "off": ("not configured", typer.colors.BRIGHT_BLACK),
}


@app.command()
def status(json: bool = typer.Option(False, "--json")) -> None:
    """Health of each integration: what it last did, and what went wrong."""
    cfg = load_config()
    conn = _conn()
    try:
        rows = store.integrations(conn, cfg)
    finally:
        conn.close()
    if json:
        _echo_json(rows)
        return

    for row in rows:
        label, color = STATE_LABEL.get(row["state"], (row["state"], None))
        typer.echo(f"{row['source']:<10} ", nl=False)
        typer.secho(f"{label:<14}", fg=color, nl=False)
        typer.echo(f" {row['open_count']:>3} open   last {row['last_attempt_at'] or 'never'}")
        if row["detail"]:
            typer.echo(f"           {row['detail']}")
        if row["last_error"]:
            typer.secho(f"           {row['last_error']}", fg=typer.colors.RED)
        if row["state"] == "empty":
            typer.secho(
                "           synced fine but the provider returned 0 tasks — check the "
                "filters (workspaces / JQL / projects), not the token",
                fg=typer.colors.YELLOW,
            )


@app.command("log")
def log_cmd(
    limit: int = typer.Option(20, "--limit", "-n"),
    source: str = typer.Option(None, "--source", "-s"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Recent sync attempts, newest first."""
    conn = _conn()
    try:
        rows = store.sync_log(conn, limit=limit, source=source)
    finally:
        conn.close()
    if json:
        _echo_json(rows)
        return
    if not rows:
        typer.echo("no sync attempts recorded yet")
        return
    for row in rows:
        mark = "ok " if row["ok"] else "ERR"
        color = typer.colors.GREEN if row["ok"] else typer.colors.RED
        typer.secho(f"{mark} ", fg=color, nl=False)
        typer.echo(
            f"{row['finished_at']}  {row['source']:<9} {row['trigger'] or '?':<9} "
            f"fetched {row['fetched']:>3}  +{row['added']} ~{row['updated']} -{row['closed']}"
        )
        if row["error"]:
            typer.secho(f"    {row['error']}", fg=typer.colors.RED)


@app.command()
def summary(
    json: bool = typer.Option(True, "--json/--text", help="JSON by default: this is the agent view."),
) -> None:
    """Compact snapshot of the list. Meant to be piped into an agent."""
    conn = _conn()
    try:
        data = store.summary(conn)
    finally:
        conn.close()
    if json:
        _echo_json(data)
        return
    now = data["current"]
    typer.echo(f"now        {now['title'] if now else '(nothing)'}")
    typer.echo(
        f"list       {data['open_count']} open / {data['count']} total, "
        f"{data['pinned_count']} ordered by hand"
    )
    by_source = "  ".join(f"{k} {v}" for k, v in sorted(data["by_source"].items()))
    typer.echo(f"sources    {by_source or 'none'}")
    typer.echo(
        f"estimated  {_fmt_minutes(data['estimated_minutes'])} "
        f"({data['unestimated_count']} without an estimate)"
    )
    typer.echo(f"overdue    {data['overdue_count']}")
    typer.echo(f"due today  {data['due_today_count']}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8787, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Run the web UI and the HTTP API."""
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        _fail("uvicorn is not installed; run `uv sync` in the project directory")
    typer.echo(f"dayplan on http://{host}:{port}  (api docs at /api/docs)")
    uvicorn.run("dayplan.api:app", host=host, port=port, reload=reload)


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
