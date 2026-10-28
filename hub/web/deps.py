"""Общие зависимости веба: текущий пользователь, защита от CSRF, шаблоны."""

from datetime import date, datetime
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from hub.auth import session_user
from hub.models import User

COOKIE = "hub_session"

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _as_dt(value: date | datetime | str) -> date | datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def ddmm(value: date | datetime | str) -> str:
    return _as_dt(value).strftime("%d.%m")


def hhmm(value: datetime | str) -> str:
    return _as_dt(value).strftime("%H:%M")  # type: ignore[union-attr]


templates.env.filters["ddmm"] = ddmm
templates.env.filters["hhmm"] = hhmm


class NotAuthenticated(Exception):
    """Нет валидной сессии: обработчик отдаёт 401."""


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def session_token(request: Request) -> str | None:
    """Сначала Bearer, потом cookie; других источников нет."""
    return bearer_token(request) or request.cookies.get(COOKIE)


async def current_user(request: Request) -> User:
    token = session_token(request)
    if token is None:
        raise NotAuthenticated
    state = request.app.state
    user = await session_user(state.db, token, state.clock)
    if user is None:
        raise NotAuthenticated
    return user


async def require_write(request: Request) -> None:
    """Изменяющие запросы — только из HTMX или с Bearer (защита от чужих форм)."""
    if request.headers.get("HX-Request") != "true" and bearer_token(request) is None:
        raise HTTPException(status_code=403, detail="Нужен HX-Request или Bearer")
