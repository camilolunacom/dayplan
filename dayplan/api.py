"""HTTP API + the drag-and-drop web UI.

Every request opens its own SQLite connection: cheap, and it keeps the
CLI and the server from fighting over a shared handle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .config import load_config
from .dates import parse_day, today_str
from .db import connect

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger("dayplan")


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect(load_config().db_path)
    try:
        yield conn
    finally:
        conn.close()


def _day(value: str | None) -> str:
    try:
        return parse_day(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _periodic_sync(minutes: int) -> None:
    """Keep the cache warm when dayplan runs as a service."""
    while True:
        try:
            report = await asyncio.to_thread(store.sync, load_config())
            if report.errors:
                log.warning("scheduled sync had errors: %s", report.errors)
            else:
                log.info("scheduled sync: %s added", report.total_added)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad sync must not kill the loop
            log.exception("scheduled sync failed")
        await asyncio.sleep(minutes * 60)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # uvicorn only configures its own loggers, so opt the root logger in here:
    # without this, scheduled syncs succeed or fail invisibly in `docker logs`.
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
        )
    cfg = load_config()
    task: asyncio.Task[None] | None = None
    if cfg.sync_interval_minutes > 0:
        log.info("scheduling a sync every %s min", cfg.sync_interval_minutes)
        task = asyncio.create_task(_periodic_sync(cfg.sync_interval_minutes))
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    app = FastAPI(title="dayplan", version="0.1.0", docs_url="/api/docs", lifespan=lifespan)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        cfg = load_config()
        return {"ok": True, "sources": cfg.enabled_sources()}

    @app.get("/api/state")
    def state(
        day: str | None = Query(None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        cfg = load_config()
        target = _day(day)
        plan = store.list_tasks(conn, day=target)
        return {
            "day": target,
            "today": today_str(),
            "next": store.next_task(plan),
            "plan": plan,
            "pending": store.list_tasks(conn, unplanned=True),
            "summary": store.summary(conn, target),
            "sources": cfg.enabled_sources(),
        }

    @app.get("/api/tasks")
    def tasks(
        source: str | None = Query(None),
        day: str | None = Query(None),
        unplanned: bool = Query(False),
        include_closed: bool = Query(False),
        q: str | None = Query(None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> list[dict[str, Any]]:
        return store.list_tasks(
            conn,
            source=source,
            day=_day(day) if day else None,
            unplanned=unplanned,
            include_closed=include_closed,
            query=q,
        )

    @app.get("/api/summary")
    def summary(
        day: str | None = Query(None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        return store.summary(conn, _day(day))

    @app.post("/api/sync")
    def sync(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        sources = payload.get("sources") or None
        report = store.sync(load_config(), sources)
        return report.as_dict()

    @app.put("/api/plan/{day}/order")
    def set_order(
        day: str,
        payload: dict[str, Any] = Body(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        ids = payload.get("ids")
        if not isinstance(ids, list):
            raise HTTPException(status_code=400, detail="body needs an 'ids' array")
        try:
            final = store.set_order(conn, _day(day), [str(i) for i in ids])
        except store.ResolveError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"day": _day(day), "ids": final}

    @app.post("/api/plan/{day}/tasks")
    def add_to_day(
        day: str,
        payload: dict[str, Any] = Body(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        task_id = payload.get("task_id")
        if not task_id:
            raise HTTPException(status_code=400, detail="body needs 'task_id'")
        position = payload.get("position")
        try:
            resolved = store.resolve(conn, str(task_id))
            return store.assign(conn, resolved, _day(day), position)
        except store.ResolveError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/plan/tasks/{task_id}")
    def remove_from_plan(
        task_id: str, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict[str, Any]:
        try:
            resolved = store.resolve(conn, task_id)
        except store.ResolveError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        store.unassign(conn, resolved)
        return {"task_id": resolved, "day": None}

    @app.patch("/api/tasks/{task_id}/plan")
    def patch_plan(
        task_id: str,
        payload: dict[str, Any] = Body(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        try:
            resolved = store.resolve(conn, task_id)
        except store.ResolveError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        est = payload.get("est_minutes")
        result = store.update_plan(
            conn,
            resolved,
            note=payload.get("note"),
            est_minutes=int(est) if est is not None else None,
            done=payload.get("done"),
            day=_day(payload["day"]) if payload.get("day") else None,
        )
        return result or {}

    if STATIC_DIR.is_dir():
        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


app = create_app()
