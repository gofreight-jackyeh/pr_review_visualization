"""FastAPI routes per docs/api.md. Request/response translation only; no business logic."""

from __future__ import annotations

import ipaddress
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ...application.ports import AnalysisRequest, ContextDoc, LocalSpec
from ...application.use_cases import AppError
from ...bootstrap import Container

HOST = "0.0.0.0"  # owner decision 2026-11-11 (overrides spec §11 default); use --host 127.0.0.1 for local-only
PORT = 8766


class LocalIn(BaseModel):
    repo_path: str
    base: str
    head: str
    title: str | None = None
    body: str | None = None


class ContextDocIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str


class AnalysisIn(BaseModel):
    pr_url: str | None = None
    local: LocalIn | None = None
    context_docs: list[ContextDocIn] = Field(default_factory=list, max_length=20)
    semantic_mode: Literal["auto", "off", "light", "standard", "deep"] = "auto"


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _host_name(header: str) -> str:
    """Host header -> bare lower-case name: port dropped, IPv6 brackets removed, trailing dot ignored."""
    h = header.strip().lower()
    if h.startswith("["):
        return h[1:].partition("]")[0]
    if h.count(":") == 1:
        h = h.partition(":")[0]
    return h.rstrip(".")


def allowed_hosts_from_env() -> frozenset[str] | None:
    """PR_EXPLORER_ALLOWED_HOSTS: extra comma-separated names; "*" disables the Host check (returns None)."""
    names = {n.strip().lower().rstrip(".") for n in os.environ.get("PR_EXPLORER_ALLOWED_HOSTS", "").split(",") if n.strip()}
    return None if "*" in names else frozenset(names)


def host_allowed(header: str | None, extra: frozenset[str]) -> bool:
    """DNS-rebinding guard: a hostile page can point its own domain at this server, but cannot make the browser send
    `localhost`, an IP literal or this machine's name as Host. Those are accepted, plus the configured extra names."""
    name = _host_name(header or "")
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost") or name in extra:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    me = socket.gethostname().lower().rstrip(".").removesuffix(".local")
    return bool(me) and name.removesuffix(".local") == me


def create_app(c: Container, executor: ThreadPoolExecutor | None = None) -> FastAPI:
    app = FastAPI(title="PR Review Explorer", version="2")
    pool = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="analysis")
    app.state.executor = pool
    # Only local dev servers (e.g. Vite) may call the API cross-origin.
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
                       allow_methods=["GET", "POST"], allow_headers=["content-type"])
    extra_hosts = allowed_hosts_from_env()

    @app.middleware("http")  # added after CORS, so it runs first
    async def _check_host(request: Request, call_next):
        if extra_hosts is not None and not host_allowed(request.headers.get("host"), extra_hosts):
            return _error(400, "bad_host", "Host header not allowed; add the name to PR_EXPLORER_ALLOWED_HOSTS")
        return await call_next(request)

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return _error(exc.http_status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        return _error(400, "invalid_input", f"{'.'.join(map(str, first.get('loc', [])))}: {first.get('msg', 'invalid')}")

    @app.exception_handler(Exception)
    async def _internal(_: Request, exc: Exception):
        return _error(500, "internal", f"{type(exc).__name__}")

    @app.post("/api/analyses")
    def start_analysis(body: AnalysisIn):
        req = AnalysisRequest(
            pr_url=body.pr_url,
            local=LocalSpec(**body.local.model_dump()) if body.local else None,
            context_docs=[ContextDoc(d.name, d.content) for d in body.context_docs],
            semantic_mode=body.semantic_mode,
        )
        aid, status, cached = c.analyze.start(req)
        if not cached:
            pool.submit(c.analyze.run, aid)
        return JSONResponse({"analysis_id": aid, "status": status, "cached": cached}, status_code=200 if cached else 202)

    @app.get("/api/analyses")
    def list_analyses():
        return c.status.list()

    @app.get("/api/analyses/{analysis_id}")
    def get_analysis(analysis_id: str):
        return c.status.get(analysis_id)

    @app.get("/api/system-maps/{system_map_id}")
    def get_system_map(system_map_id: str):
        return c.get_system_map(system_map_id)

    @app.get("/api/overlays/{overlay_id}")
    def get_overlay(overlay_id: str):
        return c.get_overlay(overlay_id)

    @app.get("/api/overlays/{overlay_id}/freshness")
    def freshness(overlay_id: str):
        return c.freshness(overlay_id)

    @app.get("/api/{rest:path}")
    def api_not_found(rest: str):
        return _error(404, "not_found", f"no API route /api/{rest}")

    dist = c.frontend_dist
    if (dist / "index.html").is_file():
        root = dist.resolve()

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            candidate = (root / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
            return FileResponse(root / "index.html")  # SPA fallback

    return app


def serve(c: Container, port: int = PORT, host: str = HOST) -> None:
    import uvicorn

    removed = c.prune_worktrees()  # nothing from an earlier run is in use yet
    if removed:
        print(f"removed {len(removed)} stale worktree(s) under {c.home / 'worktrees'}")

    uvicorn.run(create_app(c), host=host, port=port, log_level="info")
