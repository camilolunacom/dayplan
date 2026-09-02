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
from .dates import today_str
from .db import connect

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger("dayplan")


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect(load_config().db_path)
    try:
        yield conn
    finally:
        conn.close()


async def _periodic_sync(minutes: int) -> None:
    """Keep the cache warm when dayplan runs as a service."""
    while True:
        try:
            report = await asyncio.to_thread(store.sync, load_config(), None, "scheduled")
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
    def state(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        cfg = load_config()
        tasks = store.ordered_tasks(conn)
        return {
            "today": today_str(),
            "current": store.next_task(tasks),
            "tasks": tasks,
            "summary": store.summary(conn),
            "sources": cfg.enabled_sources(),
            "integrations": store.integrations(conn, cfg, history=3),
        }

    @app.get("/api/integrations")
    def integrations(
        history: int = Query(5, ge=0, le=50),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> list[dict[str, Any]]:
        return store.integrations(conn, load_config(), history=history)

    @app.get("/api/sync-log")
    def sync_log(
        limit: int = Query(30, ge=1, le=200),
        source: str | None = Query(None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> list[dict[str, Any]]:
        return store.sync_log(conn, limit=limit, source=source)

    @app.get("/api/tasks")
    def tasks(
        source: str | None = Query(None),
        include_closed: bool = Query(False),
        q: str | None = Query(None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> list[dict[str, Any]]:
        return store.list_tasks(
            conn, source=source, include_closed=include_closed, query=q
        )

    @app.get("/api/summary")
    def summary(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        return store.summary(conn)

    @app.post("/api/sync")
    def sync(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        sources = payload.get("sources") or None
        report = store.sync(load_config(), sources, trigger="manual")
        return report.as_dict()

    @app.put("/api/order")
    def set_order(
        payload: dict[str, Any] = Body(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        """Pin the given ids to the head of the list, in this order."""
        ids = payload.get("ids")
        if not isinstance(ids, list):
            raise HTTPException(status_code=400, detail="body needs an 'ids' array")
        try:
            final = store.set_list_order(conn, [str(i) for i in ids])
        except store.ResolveError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ids": final}

    @app.delete("/api/order/{task_id}")
    def unpin(task_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        """Drop a manual position; the task falls back to the default order."""
        try:
            resolved = store.resolve(conn, task_id)
        except store.ResolveError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        store.unassign(conn, resolved)
        return {"task_id": resolved, "pinned": False}

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
            day=store.LIST,
        )
        return result or {}

    if STATIC_DIR.is_dir():
        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


app = create_app()
