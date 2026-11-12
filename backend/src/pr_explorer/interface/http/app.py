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
from starlette.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ...application.ports import AnalysisRequest, ContextDoc, LocalSpec
from ...application.use_cases import AppError, Forbidden
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


class QuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    selected_id: str | None = Field(default=None, max_length=300)


ASK_MODES = ("local", "all", "off")


def ask_mode_from_env() -> str:
    """PR_EXPLORER_ASK: who may ask Claude follow-up questions — local (default: clients on this machine only), all, off."""
    mode = (os.environ.get("PR_EXPLORER_ASK") or "local").strip().lower()
    if mode not in ASK_MODES:
        raise ValueError(f"PR_EXPLORER_ASK must be one of {', '.join(ASK_MODES)}; got {mode!r}")
    return mode


def workers_from_env() -> int:
    """PR_EXPLORER_WORKERS: how many analyses run at once (default 2); the rest wait in the queue."""
    raw = (os.environ.get("PR_EXPLORER_WORKERS") or "2").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 32:
        raise ValueError(f"PR_EXPLORER_WORKERS must be an integer from 1 to 32; got {raw!r}")
    return int(raw)


def is_loopback(host: str | None) -> bool:
    """The TCP peer is this machine (127.0.0.0/8, ::1, or ::ffff:127.x). Not a header: a remote client cannot fake it."""
    try:
        ip = ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_loopback


def ask_access(mode: str, request: Request) -> tuple[bool, str | None]:
    """(allowed, reason). Asking spends the operator's Claude identity and reads the PR's code, so by default only
    someone at this machine may do it."""
    if mode == "off":
        return False, "follow-up questions are turned off on this server (PR_EXPLORER_ASK=off)"
    if mode == "local" and not is_loopback(request.client.host if request.client else None):
        return False, ("follow-up questions are only allowed from this machine (PR_EXPLORER_ASK=local); "
                       "start the server with PR_EXPLORER_ASK=all to allow every client")
    return True, None


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
    pool = executor or ThreadPoolExecutor(max_workers=workers_from_env(), thread_name_prefix="analysis")
    app.state.executor = pool
    # Only local dev servers (e.g. Vite) may call the API cross-origin.
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
                       allow_methods=["GET", "POST"], allow_headers=["content-type"])
    app.add_middleware(GZipMiddleware, minimum_size=1024)  # added after CORS: wraps it, so CORS headers stay on gzipped bodies
    extra_hosts = allowed_hosts_from_env()
    ask_mode = ask_mode_from_env()

    @app.middleware("http")  # added last, so it runs first (a rejected Host is never compressed)
    async def _check_host(request: Request, call_next):
        if extra_hosts is not None and not host_allowed(request.headers.get("host"), extra_hosts):
            name = _host_name(request.headers.get("host") or "")[:253]
            return _error(400, "bad_host", f"Host {name!r} not allowed; start the server with "
                                           f"PR_EXPLORER_ALLOWED_HOSTS={name} to allow it")
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

    @app.get("/api/config")
    def config(request: Request):
        allowed, reason = ask_access(ask_mode, request)
        return {"ask": {"mode": ask_mode, "allowed": allowed, "reason": reason}}

    @app.post("/api/overlays/{overlay_id}/questions")
    def ask(overlay_id: str, body: QuestionIn, request: Request):
        allowed, reason = ask_access(ask_mode, request)
        if not allowed:
            raise Forbidden(reason or "forbidden")
        started = c.ask.start(overlay_id, body.question, body.selected_id)
        pool.submit(c.ask.run, started["question_id"])
        return JSONResponse(started, status_code=202)

    @app.get("/api/overlays/{overlay_id}/questions")
    def questions(overlay_id: str):
        if ask_mode == "off":
            raise Forbidden("follow-up questions are turned off on this server (PR_EXPLORER_ASK=off)")
        return c.ask.list(overlay_id)

    @app.get("/api/{rest:path}")
    def api_not_found(rest: str):
        return _error(404, "not_found", f"no API route /api/{rest}")

    dist = c.frontend_dist
    if (dist / "index.html").is_file():
        root = dist.resolve()

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            candidate = (root / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(root) and candidate.name != "index.html":
                # Vite puts a content hash in every asset name, so they never change
                hashed = candidate.parent.name == "assets"
                return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"} if hashed else None)
            # always revalidate the page itself, or a browser keeps the previous build after an upgrade
            return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})  # SPA fallback

    return app


def serve(c: Container, port: int = PORT, host: str = HOST) -> None:
    import uvicorn

    removed = c.prune_worktrees()  # nothing from an earlier run is in use yet
    if removed:
        print(f"removed {len(removed)} stale worktree(s) under {c.home / 'worktrees'}")
    c.fail_unfinished_questions("the server stopped before this question was answered; ask again")

    uvicorn.run(create_app(c), host=host, port=port, log_level="info")
