"""HTTP surface per docs/api-contract.md."""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from . import __version__
from .auth import Authenticator
from .backends.base import Backend, ExecResult, ExecSpec, GuestHandle, Screenshot
from .config import AtlasConfig, ConfigError, ImageCatalog
from .errors import AtlasError, Notice
from .models import CapeSubmitRequest, ExecuteRequest, OpenSessionRequest, ScreenshotRequest
from .sessions import Session, SessionManager
from .validation import validate_execute, validate_open_session

log = logging.getLogger("atlas.api")


@dataclass
class AppState:
    config: AtlasConfig | None
    catalog: ImageCatalog | None
    config_error: ConfigError | None
    auth: Authenticator | None
    sessions: SessionManager | None
    backends: dict[str, Backend]
    started: float


# --------------------------------------------------------------------------- envelope helpers

def _notices(items: list[Notice]) -> list[dict]:
    return [n.to_dict() for n in items]


def error_response(err: AtlasError, request_id: str | None = None, **extra: Any) -> JSONResponse:
    body = {"request_id": request_id, "status": "error", "errors": _notices([err.notice]), "warnings": [], **extra}
    return JSONResponse(body, status_code=err.http_status)


def _schema_error(exc: ValidationError | RequestValidationError) -> AtlasError:
    first = exc.errors()[0] if exc.errors() else {"loc": (), "msg": "invalid"}
    loc = [str(p) for p in first.get("loc", ()) if p != "body"]
    return AtlasError("SCHEMA_INVALID", f"{'.'.join(loc) or 'body'}: {first.get('msg')}",
                      field=".".join(loc), detail=first.get("msg"))


async def _parse(request: Request, model: type[BaseModel]) -> BaseModel:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise AtlasError("BAD_JSON", f"body is not valid JSON: {exc.msg}", position=exc.pos)
    if not isinstance(data, dict):
        raise AtlasError("SCHEMA_INVALID", "body must be a JSON object", field="", detail="not an object")
    # Echo request_id even when the rest of the body is rejected.
    rid = data.get("request_id")
    request.state.request_id = rid if isinstance(rid, str) else None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise _schema_error(exc)


# --------------------------------------------------------------------------- app factory

def create_app(config: AtlasConfig | None, catalog: ImageCatalog | None, backends: dict[str, Backend],
               config_error: ConfigError | None = None,
               clock: Callable[[], float] = time.monotonic, wall=None) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if state.sessions:
            await state.sessions.start_reaper()
        yield
        if state.sessions:
            await state.sessions.shutdown()

    app = FastAPI(title="Atlas", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    sessions = None
    auth = None
    if config is not None and config_error is None:
        auth = Authenticator(config.auth, clock=clock)
        kwargs = {"clock": clock}
        if wall is not None:
            kwargs["wall"] = wall
        sessions = SessionManager(config.limits, backends, **kwargs)

    state = AppState(config, catalog, config_error, auth, sessions, backends, time.time())
    app.state.atlas = state

    # ---------------------------------------------------------------- error handling

    @app.exception_handler(AtlasError)
    async def _atlas_error(request: Request, exc: AtlasError) -> JSONResponse:
        return error_response(exc, request_id=getattr(request.state, "request_id", None))

    @app.exception_handler(RequestValidationError)
    async def _req_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(_schema_error(exc), request_id=getattr(request.state, "request_id", None))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        trace_id = uuid.uuid4().hex[:16]
        log.exception("unhandled error trace_id=%s", trace_id)
        return error_response(AtlasError("INTERNAL", "unhandled exception", trace_id=trace_id))

    # ---------------------------------------------------------------- guards

    def guard(request: Request) -> tuple[AtlasConfig, ImageCatalog, SessionManager]:
        """Config valid + authenticated. Every route except /v1/health goes through this."""
        if state.config_error is not None or state.config is None or state.catalog is None:
            raise AtlasError("CONFIG_INVALID", "Atlas started with an invalid config; only /v1/health is served",
                             problems=state.config_error.problems if state.config_error else [])
        if state.sessions and state.sessions.shutting_down:
            raise AtlasError("SHUTTING_DOWN", "Atlas is restarting")
        assert state.auth and state.sessions
        state.auth.authenticate(request.headers.get("authorization"))
        return state.config, state.catalog, state.sessions

    # ---------------------------------------------------------------- routes

    @app.get("/v1/health")
    async def health() -> dict:
        return {
            "status": "ok" if state.config_error is None else "degraded",
            "version": __version__,
            "uptime_sec": int(time.time() - state.started),
            "config": "ok" if state.config_error is None else "invalid",
            "sessions_active": len(state.sessions.sessions) if state.sessions else 0,
        }

    @app.get("/v1/images")
    async def images(request: Request) -> dict:
        cfg, catalog, mgr = guard(request)
        busy = mgr.busy_entry_ids()
        out = []
        for e in catalog.images:
            if not e.enabled:
                st = "unavailable"
            elif (e.runtime, e.id) in busy:
                st = "busy"
            else:
                backend = state.backends.get(e.runtime)
                st = await backend.entry_state(e) if backend else "unavailable"
            item = {"runtime": e.runtime, "id": e.id, "build": e.build, "state": st}
            if e.default:
                item["default"] = True
            if e.deprecated:
                item["deprecated"] = True
                item["removal_date"] = e.removal_date
            out.append(item)
        lim = cfg.limits
        return {"images": out, "limits": {
            "max_timeout_sec": lim.max_timeout_sec, "session_idle_sec": lim.session_idle_sec,
            "session_idle_warn_sec": lim.session_idle_warn_sec, "max_sessions": lim.max_sessions,
            "max_upload_bytes": lim.max_upload_bytes, "max_files": lim.max_files,
        }}

    @app.post("/v1/execute")
    async def execute(request: Request) -> JSONResponse:
        cfg, catalog, mgr = guard(request)
        req: ExecuteRequest = await _parse(request, ExecuteRequest)  # type: ignore[assignment]
        request.state.request_id = req.request_id
        try:
            return await _execute(cfg, catalog, mgr, req)
        except AtlasError as exc:
            return error_response(exc, request_id=req.request_id, runtime=req.runtime, version=req.version)

    async def _execute(cfg: AtlasConfig, catalog: ImageCatalog, mgr: SessionManager, req: ExecuteRequest) -> JSONResponse:
        r = validate_execute(req, cfg, catalog)
        warnings: list[Notice] = list(r.warnings)
        spec = ExecSpec(req.language, req.code, req.entrypoint, req.args, req.env, req.stdin, r.timeout_sec, r.files)
        backend = state.backends[r.entry.runtime]

        session: Session | None = None
        handle: GuestHandle
        one_shot = False

        if req.session_id:
            session = await mgr.get(req.session_id)
            mgr.check_compatible(session, r.entry, r.network)
            handle = session.handle
        elif r.mode == "session":
            session, w = await mgr.open(r.entry, r.network)
            warnings += w
            handle = session.handle
        else:
            one_shot = True
            if r.entry.is_vm:
                session, w = await mgr.open(r.entry, r.network)
                warnings += w
                handle = session.handle
            else:
                handle = await backend.open(r.entry, r.network)

        shot: Screenshot | None = None
        try:
            if session is not None:
                async with mgr.acquire(session):
                    warnings += mgr.touch(session)
                    result = await backend.execute(handle, spec)
                    if req.screenshot.requested and _want_shot(req, result):
                        shot = await _safe_shot(backend, handle, req.screenshot.source, warnings)
            else:
                result = await backend.execute(handle, spec)
        finally:
            if one_shot:
                if session is not None:
                    await mgr.close(session.id)
                    session = None
                else:
                    await backend.close(handle)

        warnings += result.warnings
        body: dict[str, Any] = {
            "request_id": req.request_id,
            "status": result.status,
            "runtime": r.entry.runtime,
            "version": r.entry.id if r.entry.runtime != "ephemeral" else None,
            "build": r.entry.build,
            "session_id": session.id if session else None,
            "session": session.block(cfg.limits.session_idle_sec, mgr.clock()) if session else None,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "wall_time_ms": result.wall_time_ms,
            "cpu_time_ms": result.cpu_time_ms,
            "peak_memory_bytes": result.peak_memory_bytes,
            "network": r.network.to_dict(),
            "screenshot": _shot_dict(shot),
            "warnings": _notices(warnings),
            "errors": _notices(result.errors),
        }
        return JSONResponse(body, status_code=200)

    @app.post("/v1/sessions")
    async def open_session(request: Request) -> JSONResponse:
        cfg, catalog, mgr = guard(request)
        req: OpenSessionRequest = await _parse(request, OpenSessionRequest)  # type: ignore[assignment]
        request.state.request_id = req.request_id
        entry, network, warnings = validate_open_session(req, catalog)
        s, w = await mgr.open(entry, network)
        warnings += w
        return JSONResponse({
            "request_id": req.request_id,
            "session_id": s.id, "runtime": s.runtime, "version": s.entry.id, "build": s.entry.build,
            "state": s.state, "network": network.to_dict(),
            "session": s.block(cfg.limits.session_idle_sec, mgr.clock()),
            "warnings": _notices(warnings), "errors": [],
        }, status_code=201)

    @app.get("/v1/sessions/{session_id}")
    async def session_status(session_id: str, request: Request) -> dict:
        cfg, catalog, mgr = guard(request)
        s = await mgr.get(session_id)
        return {
            "session_id": s.id, "runtime": s.runtime, "version": s.entry.id, "build": s.entry.build,
            "state": s.state, "network": s.network.to_dict(),
            "session": s.block(cfg.limits.session_idle_sec, mgr.clock()),
            "warnings": _notices(mgr.idle_warnings(s)), "errors": [],
        }

    @app.delete("/v1/sessions/{session_id}")
    async def close_session(session_id: str, request: Request) -> dict:
        cfg, catalog, mgr = guard(request)
        reverted = await mgr.close(session_id)
        return {"session_id": session_id, "state": "closed", "reverted": bool(reverted)}

    @app.post("/v1/sessions/{session_id}/screenshot")
    async def session_screenshot(session_id: str, request: Request) -> dict:
        cfg, catalog, mgr = guard(request)
        req: ScreenshotRequest = await _parse(request, ScreenshotRequest)  # type: ignore[assignment]
        s = await mgr.get(session_id)
        if req.source != "guest" and (req.window_title or req.region):
            raise AtlasError("SCHEMA_INVALID", "window_title/region require source: guest",
                             field="source", detail="window_title and region only work with source: guest")
        backend = state.backends[s.runtime]
        async with mgr.acquire(s):
            mgr.touch(s)
            s.executions -= 1  # screenshots aren't executions
            shot = await backend.screenshot(s.handle, req.source, req.window_title, req.region)
        d = _shot_dict(shot) or {}
        d["warnings"] = _notices(shot.warnings)
        d["errors"] = []
        return d

    @app.post("/v1/cape/submit")
    async def cape_submit(request: Request) -> JSONResponse:
        cfg, catalog, mgr = guard(request)
        req: CapeSubmitRequest = await _parse(request, CapeSubmitRequest)  # type: ignore[assignment]
        if "cape" not in state.backends or not cfg.cape.enabled:
            raise AtlasError("CAPE_UNREACHABLE", "CAPE is not configured on this Atlas")
        raise AtlasError("CAPE_UNREACHABLE", "CAPE relay not implemented yet")

    @app.get("/v1/cape/{task_id}")
    async def cape_status(task_id: str, request: Request) -> dict:
        guard(request)
        raise AtlasError("CAPE_TASK_NOT_FOUND", f"unknown task {task_id!r}", task_id=task_id)

    return app


# --------------------------------------------------------------------------- small helpers

def _want_shot(req: ExecuteRequest, result: ExecResult) -> bool:
    if result.status == "ok":
        return req.screenshot.on_complete
    return req.screenshot.on_error or req.screenshot.on_complete


async def _safe_shot(backend: Backend, handle: GuestHandle, source: str, warnings: list[Notice]) -> Screenshot | None:
    try:
        shot = await backend.screenshot(handle, source, None, None)
        warnings.extend(shot.warnings)
        return shot
    except AtlasError as exc:
        # A failed screenshot must not turn a successful execution into an HTTP error.
        warnings.append(Notice("SCREENSHOT_GUEST_FALLBACK", f"screenshot failed: {exc.notice.message}",
                               {"error": exc.code, **exc.notice.data}))
        return None


def _shot_dict(shot: Screenshot | None) -> dict | None:
    if shot is None:
        return None
    return {"source": shot.source, "format": shot.format, "width": shot.width,
            "height": shot.height, "content_b64": shot.content_b64}
