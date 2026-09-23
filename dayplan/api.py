"""HTTP API + the drag-and-drop web UI.

Every request opens its own SQLite connection: cheap, and it keeps the
CLI and the server from fighting over a shared handle.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .config import load_config
from .dates import today_str
from .db import connect

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger("dayplan")

# Assets whose URL carries a version, so a deploy can never be served from a
# stale cache. Anything not listed here is served unversioned and revalidated.
VERSIONED_ASSETS = ("style.css", "app.js")


def _asset_fingerprint() -> tuple[str, float]:
    """Short content hash of the versioned assets, plus their newest mtime.

    The mtime is what lets a `--reload` dev server notice an edit without
    re-hashing on every single request.
    """
    digest = hashlib.sha256()
    newest = 0.0
    for name in VERSIONED_ASSETS:
        path = STATIC_DIR / name
        if not path.is_file():
            continue
        digest.update(path.read_bytes())
        newest = max(newest, path.stat().st_mtime)
    return digest.hexdigest()[:12], newest


_index_cache: dict[str, object] = {}


def render_index() -> str:
    """index.html with ?v=<hash> on each versioned asset."""
    path = STATIC_DIR / "index.html"
    version, newest = _asset_fingerprint()
    stamp = max(newest, path.stat().st_mtime if path.is_file() else 0.0)
    if _index_cache.get("stamp") != stamp:
        html = path.read_text(encoding="utf-8")
        for name in VERSIONED_ASSETS:
            html = html.replace(f"/static/{name}", f"/static/{name}?v={version}")
        _index_cache.update({"stamp": stamp, "html": html})
    return str(_index_cache["html"])


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
        snapshot = store.state_snapshot(conn, cfg, history=3)
        return {
            "today": today_str(),
            "sources": cfg.enabled_sources(),
            **snapshot,
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
        """Pin the given ids to the head of the list, in this order.

        Requires the order revision the caller last read `/api/state` with:
        a stale one (the order changed elsewhere since, e.g. a task
        confirmed-reopened and dropped its old plan row) is rejected with 409
        rather than silently overwritten.
        """
        ids = payload.get("ids")
        if not isinstance(ids, list):
            raise HTTPException(status_code=400, detail="body needs an 'ids' array")
        revision = payload.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise HTTPException(status_code=400, detail="body needs an integer 'revision'")
        try:
            final = store.set_order(conn, [str(i) for i in ids], expected_revision=revision)
        except store.ResolveError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except store.ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ids": final, "revision": store.get_order_revision(conn)}

    @app.post("/api/accept")
    def accept(
        payload: dict[str, Any] = Body(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        """Move a new task into the main list, unranked."""
        task_id = payload.get("task_id")
        if not task_id:
            raise HTTPException(status_code=400, detail="body needs 'task_id'")
        try:
            resolved = store.resolve(conn, str(task_id))
            store.acknowledge(conn, resolved)
        except store.ResolveError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"task_id": resolved, "zone": "unordered"}

    @app.post("/api/dismiss")
    def dismiss(
        payload: dict[str, Any] = Body(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, Any]:
        """Send a task back to the new pile, discarding its note and estimate."""
        task_id = payload.get("task_id")
        if not task_id:
            raise HTTPException(status_code=400, detail="body needs 'task_id'")
        try:
            resolved = store.resolve(conn, str(task_id))
        except store.ResolveError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        store.unacknowledge(conn, resolved)
        return {"task_id": resolved, "zone": "new"}

    @app.delete("/api/order/{task_id}")
    def unpin(task_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
        """Drop a manual position; the task falls back to the default order."""
        try:
            resolved = store.resolve(conn, task_id)
        except store.ResolveError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        store.unassign(conn, resolved)
        return {"task_id": resolved, "pinned": False}

    if STATIC_DIR.is_dir():
        @app.middleware("http")
        async def asset_cache_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
            response = await call_next(request)
            if request.url.path.startswith("/static/"):
                # A versioned URL is safe to cache forever: the URL itself
                # changes when the file does. Anything unversioned (the icon
                # the ZimaOS tile points at) must revalidate instead.
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                    if "v" in request.query_params
                    else "no-cache"
                )
            return response

        @app.get("/")
        def index() -> HTMLResponse:
            # The document itself must always revalidate, otherwise a cached
            # copy would keep pointing at the previous asset version.
            return HTMLResponse(
                render_index(), headers={"Cache-Control": "no-cache"}
            )

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


app = create_app()
