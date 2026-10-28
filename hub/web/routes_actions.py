"""Действия над записями: каждое возвращает обновлённую строку (HTMX подменяет её на месте)."""

from datetime import timedelta
from typing import Annotated, get_args
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from hub.access import PrincipalType, share
from hub.calendar_ics import CalendarError, fetch_ics, parse_events_safe, sync_connection
from hub.connections import add_connection, disable_connection, list_connections
from hub.entries import Entry, complete, drop, get_entry, reopen, set_section
from hub.models import EntryKind, User
from hub.service import NOT_PARSED
from hub.users import create_invite, set_brief_time
from hub.web.deps import current_user, templates
from hub.web.routes_pages import conn_view, make_menu, make_rows, settings_view, tasks_view

router = APIRouter()
UserDep = Annotated[User, Depends(current_user)]


async def _row(request: Request, user: User, entry: Entry, toast: str | None = None) -> Response:
    """Строка записи; тост (если есть) уходит рядом как out-of-band."""
    menu = await make_menu(request, user)
    (row,) = await make_rows(request, menu, [entry])
    html = templates.get_template("partials/task_row.html").render(row=row, menu=menu)
    if toast:
        html += templates.get_template("partials/toast.html").render(toast=toast)
    # перерисовать надо только плитки: сам виджет «Горит» остаётся, чтобы можно было вернуть
    return HTMLResponse(html, headers={"HX-Trigger": "kpi-changed"})


@router.post("/entries", response_class=HTMLResponse)
async def add_entry(
    request: Request,
    user: UserDep,
    text: Annotated[str, Form()],
    f: Annotated[str, Form()] = "all",
    s: Annotated[str, Form()] = "",
    view: Annotated[str, Form()] = "tasks",
) -> Response:
    service = request.app.state.service
    entry, note = await service.add_entry(user, text)
    if note is not None and note.startswith(NOT_PARSED):
        toast = note
    else:
        toast = await service.describe(entry) + (f"\n{note}" if note else "")
    if view == "dash":  # быстрая запись с главной: тост, виджеты перерисуются сами
        html = templates.get_template("partials/toast.html").render(toast=toast)
        return HTMLResponse(html, headers={"HX-Trigger": "entries-changed"})
    tasks = await tasks_view(request, user, f, s)
    return templates.TemplateResponse(
        request, "partials/task_groups.html", {"view": tasks, "menu": tasks.menu, "toast": toast}
    )


@router.post("/brief", response_class=HTMLResponse)
async def build_brief(request: Request, user: UserDep) -> Response:
    """Единственное место, где главная зовёт модель, — по нажатию кнопки."""
    intro = await request.app.state.service.brief_intro(user)
    return templates.TemplateResponse(
        request, "partials/widgets/brief.html", {"intro": intro, "status": None, "tried": True}
    )


@router.post("/entries/{entry_id}/done", response_class=HTMLResponse)
async def done(request: Request, user: UserDep, entry_id: int) -> Response:
    state = request.app.state
    return await _row(request, user, await complete(state.db, user, entry_id, state.clock))


@router.post("/entries/{entry_id}/drop", response_class=HTMLResponse)
async def drop_entry(request: Request, user: UserDep, entry_id: int) -> Response:
    state = request.app.state
    return await _row(request, user, await drop(state.db, user, entry_id, state.clock))


@router.post("/entries/{entry_id}/reopen", response_class=HTMLResponse)
async def reopen_entry(request: Request, user: UserDep, entry_id: int) -> Response:
    return await _row(request, user, await reopen(request.app.state.db, user, entry_id))


@router.post("/entries/{entry_id}/kind/{kind}", response_class=HTMLResponse)
async def kind(request: Request, user: UserDep, entry_id: int, kind: EntryKind) -> Response:
    entry, note = await request.app.state.service.set_kind(user, entry_id, kind)
    return await _row(request, user, entry, note)


@router.post("/entries/{entry_id}/section/{section_id}", response_class=HTMLResponse)
async def section(request: Request, user: UserDep, entry_id: int, section_id: int) -> Response:
    entry = await set_section(request.app.state.db, user, entry_id, section_id or None)
    return await _row(request, user, entry)


@router.post("/entries/{entry_id}/share", response_class=HTMLResponse)
async def share_entry(
    request: Request, user: UserDep, entry_id: int, to: Annotated[str, Form()]
) -> Response:
    principal, _, raw = to.partition(":")
    if principal not in get_args(PrincipalType) or not raw.isdecimal():
        raise LookupError("получатель не найден")
    state = request.app.state
    await share(state.db, user, "entry", entry_id, principal, int(raw), state.clock)  # type: ignore[arg-type]
    return await _row(request, user, await get_entry(state.db, user, entry_id))


@router.post("/entries/{entry_id}/subtasks", response_class=HTMLResponse)
async def subtasks(request: Request, user: UserDep, entry_id: int) -> Response:
    state = request.app.state
    text = await state.service.create_subtasks(user, entry_id)
    return await _row(request, user, await get_entry(state.db, user, entry_id), text)


NAME_MAX = 60


def _render(template: str, **ctx) -> str:
    return templates.get_template(template).render(**ctx)


async def _connections_block(
    request: Request, user: User, error: str | None = None, name: str = "", toast: str | None = None
) -> Response:
    view = await settings_view(request, user)
    html = _render("partials/connections_block.html", view=view, error=error, name=name)
    if toast:
        html += _render("partials/toast.html", toast=toast)
    return HTMLResponse(html)


async def _conn_row(request: Request, user: User, connection_id: int, toast: str) -> Response:
    state = request.app.state
    (conn,) = [c for c in await list_connections(state.db, user) if c.id == connection_id]
    html = _render("partials/connection_row.html", c=conn_view(conn, state.clock.now()))
    return HTMLResponse(html + _render("partials/toast.html", toast=toast))


@router.post("/connections", response_class=HTMLResponse)
async def add_calendar(
    request: Request,
    user: UserDep,
    name: Annotated[str, Form()] = "",
    url: Annotated[str, Form()] = "",
) -> Response:
    """Сначала пробная загрузка и разбор; сохраняем, только если календарь читается."""
    state = request.app.state
    name, url = name.strip(), url.strip()
    if not 1 <= len(name) <= NAME_MAX:
        return await _connections_block(
            request, user, f"Название — от 1 до {NAME_MAX} знаков", name
        )
    try:
        async with state.http_client_factory() as client:
            today = state.clock.today()
            raw = await fetch_ics(url, client, state.resolver)
            await parse_events_safe(raw, today - timedelta(days=1), today + timedelta(days=7))
            host = urlsplit(url).hostname or ""
            conn = await add_connection(
                state.db, state.box, user, "ics_calendar", name, url, host, state.clock
            )
            await sync_connection(state.db, state.box, conn, client, state.clock, state.resolver)
    except CalendarError as exc:
        # текст ошибок календаря безопасен; ссылку обратно в форму не возвращаем
        return await _connections_block(request, user, str(exc), name)
    return await _connections_block(request, user, toast=f"Календарь «{name}» подключён")


@router.post("/connections/{connection_id}/check", response_class=HTMLResponse)
async def check_connection(request: Request, user: UserDep, connection_id: int) -> Response:
    state = request.app.state
    conn = next((c for c in await list_connections(state.db, user) if c.id == connection_id), None)
    if conn is None:
        raise LookupError("подключение не найдено")
    async with state.http_client_factory() as client:
        await sync_connection(state.db, state.box, conn, client, state.clock, state.resolver)
    return await _conn_row(request, user, connection_id, f"Проверено: «{conn.name}»")


@router.post("/connections/{connection_id}/disable", response_class=HTMLResponse)
async def disable(request: Request, user: UserDep, connection_id: int) -> Response:
    state = request.app.state
    await disable_connection(state.db, user, connection_id, state.clock)
    return await _conn_row(request, user, connection_id, "Подключение отключено")


@router.post("/settings/brief-time", response_class=HTMLResponse)
async def brief_time(
    request: Request, user: UserDep, brief_time: Annotated[str, Form()] = ""
) -> Response:
    state = request.app.state
    error = None
    try:
        user = await set_brief_time(state.db, user, brief_time.strip())
    except ValueError as exc:
        error = str(exc)
    else:
        if state.on_brief_time_changed:
            await state.on_brief_time_changed(user)
    view = await settings_view(request, user)
    html = _render("partials/brief_time.html", view=view, error=error)
    if error is None:
        html += _render("partials/toast.html", toast=f"Брифинг теперь в {user.brief_time}")
    return HTMLResponse(html)


@router.post("/invites", response_class=HTMLResponse)
async def invite(request: Request, user: UserDep) -> Response:
    state = request.app.state
    code = await create_invite(state.db, user, state.clock)  # не админ → PermissionError → 404
    bot = state.settings.bot_username
    link = f"https://t.me/{bot}?start={code}" if bot else None
    return HTMLResponse(_render("partials/invite.html", code=code, link=link))
