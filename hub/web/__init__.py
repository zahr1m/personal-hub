"""Веб-панель: FastAPI + Jinja2 + HTMX в том же процессе, что и бот."""

from collections.abc import Awaitable, Callable
from pathlib import Path

import aiosqlite
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from hub.calendar_ics import Resolver
from hub.clock import Clock
from hub.config import Settings
from hub.crypto import SecretBox
from hub.models import User
from hub.service import Service
from hub.web import routes_actions, routes_auth, routes_pages
from hub.web.deps import NotAuthenticated, bearer_token, templates

STATIC_DIR = Path(__file__).parent / "static"

CSP = (
    "frame-ancestors 'self' https://web.telegram.org; default-src 'self'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data:"
)


def add_security_headers(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def create_app(
    db: aiosqlite.Connection,
    service: Service,
    settings: Settings,
    clock: Clock,
    box: SecretBox,
    http_client_factory: Callable[[], httpx.AsyncClient],
    on_brief_time_changed: Callable[[User], Awaitable[None]] | None = None,
    resolver: Resolver | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # box, http_client_factory и resolver нужны «Настройкам»; resolver None — настоящий DNS
    app.state.db = db
    app.state.service = service
    app.state.settings = settings
    app.state.clock = clock
    app.state.box = box
    app.state.http_client_factory = http_client_factory
    app.state.on_brief_time_changed = on_brief_time_changed
    app.state.resolver = resolver

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        """Изменяющие запросы — только из HTMX или с Bearer; исключение — вход через initData."""
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and not (request.method == "POST" and request.url.path == "/auth/tma")
            and request.headers.get("HX-Request") != "true"
            and bearer_token(request) is None
        ):
            return Response("Нужен HX-Request или Bearer", status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        return add_security_headers(await call_next(request))

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> Response:
        """500 отдаёт внешний ServerErrorMiddleware, мимо security_headers."""
        return add_security_headers(Response("Internal Server Error", status_code=500))

    @app.exception_handler(NotAuthenticated)
    async def not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=401, headers={"HX-Trigger": "reauth"})
        return templates.TemplateResponse(
            request,
            "message.html",
            {"title": "Вход", "text": "Войдите: напишите боту /web", "login": True},
            status_code=401,
        )

    @app.exception_handler(PermissionError)
    @app.exception_handler(LookupError)
    async def no_access(request: Request, exc: Exception) -> Response:
        """Нет прав и нет записи неразличимы: 404 без названий чужих записей."""
        if isinstance(exc, KeyError):
            raise exc
        return HTMLResponse("Нет доступа", status_code=404)

    app.include_router(routes_auth.router)
    app.include_router(routes_pages.router)
    app.include_router(routes_actions.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=True), name="static")
    return app
