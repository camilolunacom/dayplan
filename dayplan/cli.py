"""dayplan CLI. Talks straight to SQLite, so no server has to be running."""

from __future__ import annotations

import json as jsonlib
import sqlite3
import sys
from typing import Any

import typer

from . import store
from .config import load_config
from .dates import parse_day, today_str
from .db import connect, get_meta

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
    for index, task in enumerate(tasks, start=1):
        if numbered:
            mark = "x" if task["done"] else " "
            typer.echo(_task_line(task, prefix=f"{index:>2}. [{mark}] "))
        else:
            typer.echo(_task_line(task, prefix="    "))


# --------------------------------------------------------------------------- commands


@app.command()
def doctor() -> None:
    """Show which sources are configured and where the data lives."""
    cfg = load_config()
    typer.echo(f"database        {cfg.db_path}")
    typer.echo(f"ticktick        {'ok' if cfg.ticktick_token else 'MISSING'}  ({cfg.ticktick_token_source})")
    typer.echo(f"asana           {'ok' if cfg.asana_token else 'MISSING'}  (ASANA_TOKEN)")
    workspaces = ", ".join(cfg.asana_workspaces) or "all visible"
    typer.echo(f"  workspaces    {workspaces}")
    typer.echo(f"jira            {'ok' if cfg.jira_ready else 'MISSING'}  ({cfg.jira_base_url or 'JIRA_BASE_URL unset'})")
    typer.echo(f"  jql           {cfg.jira_jql}")
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
    report = store.sync(load_config(), list(source) if source else None)
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
    day: str = typer.Option(None, "--day", "-d", help="Only tasks planned for this day."),
    pending: bool = typer.Option(False, "--pending", "-p", help="Only unplanned tasks."),
    query: str = typer.Option(None, "--query", "-q", help="Substring of title or project."),
    all_: bool = typer.Option(False, "--all", "-a", help="Include closed tasks."),
    limit: int = typer.Option(0, "--limit", "-n"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """List tasks."""
    conn = _conn()
    try:
        tasks = store.list_tasks(
            conn,
            source=source,
            day=parse_day(day) if day else None,
            unplanned=pending,
            include_closed=all_,
            query=query,
        )
    finally:
        conn.close()
    if limit > 0:
        tasks = tasks[:limit]
    if json:
        _echo_json(tasks)
        return
    typer.echo(f"{len(tasks)} task(s)")
    _print_tasks(tasks, numbered=bool(day))


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
def plan(
    day: str = typer.Argument("today", help="today, tomorrow, +2 or YYYY-MM-DD"),
    json: bool = typer.Option(False, "--json"),
) -> None:
    """Show the ordered plan for a day."""
    target = parse_day(day)
    conn = _conn()
    try:
        tasks = store.list_tasks(conn, day=target)
    finally:
        conn.close()
    if json:
        _echo_json({"day": target, "plan": tasks})
        return
    est = _fmt_minutes(sum(t["est_minutes"] or 0 for t in tasks if not t["done"]))
    open_count = len([t for t in tasks if not t["done"]])
    typer.echo(f"Plan {target} — {open_count} open of {len(tasks)}, estimated {est}")
    _print_tasks(tasks, numbered=True)


@app.command()
def today(json: bool = typer.Option(False, "--json")) -> None:
    """Shortcut for `dayplan plan today`."""
    plan(today_str(), json)


@app.command()
def add(
    refs: list[str] = typer.Argument(..., help="Task refs (#12, TN-1171, id, or title text)."),
    day: str = typer.Option("today", "--day", "-d"),
    position: int = typer.Option(None, "--pos", help="0-based insert index; default appends."),
) -> None:
    """Put tasks on a day."""
    target = parse_day(day)
    conn = _conn()
    try:
        for offset, task_id in enumerate(_resolve_many(conn, refs)):
            pos = None if position is None else position + offset
            result = store.assign(conn, task_id, target, pos)
            typer.echo(f"{task_id} -> {target} #{result['position'] + 1}")
    finally:
        conn.close()


@app.command()
def order(
    day: str = typer.Argument(..., help="today, tomorrow or YYYY-MM-DD"),
    refs: list[str] = typer.Argument(..., help="Task refs, in the order you want them."),
) -> None:
    """Set the exact order of a day. Tasks not listed stay, appended after."""
    target = parse_day(day)
    conn = _conn()
    try:
        final = store.set_order(conn, target, _resolve_many(conn, refs))
        tasks = store.list_tasks(conn, day=target)
    finally:
        conn.close()
    typer.echo(f"Plan {target} — {len(final)} task(s)")
    _print_tasks(tasks, numbered=True)


@app.command()
def move(
    ref: str,
    day: str = typer.Option(None, "--day", "-d"),
    position: int = typer.Option(None, "--pos", help="1-based slot in the day."),
) -> None:
    """Move a task to another day and/or another slot."""
    if day is None and position is None:
        _fail("give at least --day or --pos")
    conn = _conn()
    try:
        task_id = _resolve_many(conn, [ref])[0]
        current = store.get_task(conn, task_id) or {}
        target = parse_day(day) if day else (current.get("day") or today_str())
        index = None if position is None else max(0, position - 1)
        result = store.assign(conn, task_id, target, index)
        typer.echo(f"{task_id} -> {target} #{result['position'] + 1}")
    finally:
        conn.close()


@app.command()
def drop(refs: list[str] = typer.Argument(...)) -> None:
    """Take tasks off the calendar (they go back to the pending pool)."""
    conn = _conn()
    try:
        for task_id in _resolve_many(conn, refs):
            store.unassign(conn, task_id)
            typer.echo(f"{task_id} -> pending")
    finally:
        conn.close()


@app.command()
def note(ref: str, text: str = typer.Argument(..., help="Use '' to clear.")) -> None:
    """Attach a local planning note to a task."""
    conn = _conn()
    try:
        task_id = _resolve_many(conn, [ref])[0]
        store.update_plan(conn, task_id, note=text)
        typer.echo(f"{task_id} note set")
    finally:
        conn.close()


@app.command()
def est(ref: str, minutes: int = typer.Argument(..., help="0 clears the estimate.")) -> None:
    """Set a time estimate, in minutes."""
    conn = _conn()
    try:
        task_id = _resolve_many(conn, [ref])[0]
        store.update_plan(conn, task_id, est_minutes=minutes)
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
            store.update_plan(conn, task_id, done=not undo)
            typer.echo(f"{task_id} {'reopened' if undo else 'done (local only)'}")
    finally:
        conn.close()


@app.command()
def summary(
    day: str = typer.Option("today", "--day", "-d"),
    json: bool = typer.Option(True, "--json/--text", help="JSON by default: this is the agent view."),
) -> None:
    """Compact snapshot of the workload. Meant to be piped into an agent."""
    conn = _conn()
    try:
        data = store.summary(conn, parse_day(day))
    finally:
        conn.close()
    if json:
        _echo_json(data)
        return
    typer.echo(f"Day {data['day']}")
    typer.echo(f"  planned    {data['plan_open']} open / {data['plan_count']} total, {_fmt_minutes(data['plan_estimated_minutes'])}")
    by_source = "  ".join(f"{k} {v}" for k, v in sorted(data["pending_by_source"].items()))
    typer.echo(f"  pending    {data['pending_count']}  ({by_source or 'none'})")
    typer.echo(f"  overdue    {data['overdue_count']}")
    typer.echo(f"  due today  {data['due_today_count']}")


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
