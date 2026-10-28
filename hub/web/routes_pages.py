"""Страницы веба: главная-дашборд, виджеты, «Задачи», «Настройки», заглушки."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from hub.access import sharing_counts
from hub.brief import ALL_DAY, event_time, today_brief
from hub.budget import usage_for_day
from hub.calendar_ics import events_for_day
from hub.connections import Connection, list_connections
from hub.entries import (
    Entry,
    children,
    list_open_projects,
    list_open_tasks,
    list_think,
    shared_ids,
)
from hub.models import User
from hub.sections import Section, list_sections
from hub.users import BRIEF_FROM, BRIEF_TO, family_space_id, list_users
from hub.web.deps import current_user, templates

router = APIRouter()

Tone = Literal["error", "warning", "neutral"]
FILTERS = ("mine", "shared", "all")
FILTER_LABELS = {"mine": "Мои", "shared": "Общие", "all": "Все"}
WEEK = 7


def deadline_badge(due: date, today: date) -> tuple[str, Tone]:
    """Плашка срока: просрочено — красный, сегодня и до 7 дней — янтарный, дальше — серый."""
    days = (today - due).days
    if days == 1:
        return "вчера", "error"
    if days > 1:
        return f"просрочено {days} дн.", "error"
    if days == 0:
        return "сегодня", "warning"
    return f"{due:%d.%m}", "warning" if -days <= WEEK else "neutral"


@dataclass
class Menu:
    """Всё, что нужно меню действий строки: одно на запрос."""

    user: User
    today: date
    sections: list[Section]
    section_names: dict[int, str]
    own_sections: list[Section]
    others: list[User]
    family_id: int


@dataclass
class Row:
    entry: Entry
    mine: bool
    section: str | None
    shared: bool
    badge: tuple[str, Tone] | None
    progress: tuple[int, int] | None  # (сделано, всего) у проекта
    can_create_subtasks: bool


async def make_menu(request: Request, user: User) -> Menu:
    state = request.app.state
    sections = await list_sections(state.db, user)
    return Menu(
        user,
        state.clock.today(),
        sections,
        {s.id: s.name for s in sections},
        [s for s in sections if s.owner_id == user.id],
        [u for u in await list_users(state.db) if u.id != user.id],
        await family_space_id(state.db),
    )


async def make_rows(request: Request, menu: Menu, entries: list[Entry]) -> list[Row]:
    state = request.app.state
    user = menu.user
    shared = await shared_ids(state.db, user, entries) if entries else set()
    rows = []
    for e in entries:
        progress = None
        can_create = False
        if e.kind == "project":
            kids = await children(state.db, user, e.id)
            progress = (sum(k.status == "done" for k in kids), len(kids))
            can_create = bool(await state.service.proposal(user, e.id))
        rows.append(
            Row(
                e,
                e.owner_id == user.id,
                menu.section_names.get(e.section_id) if e.section_id is not None else None,
                e.id in shared,
                deadline_badge(e.due_date, menu.today) if e.due_date else None,
                progress,
                can_create,
            )
        )
    return rows


def _group_key(e: Entry, today: date) -> str:
    if e.due_date is None:
        return "none"
    if e.due_date < today:
        return "overdue"
    if e.due_date == today:
        return "today"
    return "week" if e.due_date <= today + timedelta(days=WEEK) else "later"


GROUPS = (
    ("overdue", "Просрочено"),
    ("today", "Сегодня"),
    ("week", "На неделе"),
    ("later", "Позже"),
    ("none", "Без срока"),
)


def _by_filter(entries: list[Entry], user: User, shared: set[int], f: str) -> list[Entry]:
    if f == "mine":
        return [e for e in entries if e.owner_id == user.id]
    if f == "shared":
        return [e for e in entries if e.id in shared]
    return entries


@dataclass
class TasksView:
    menu: Menu
    f: str
    section_id: int | None
    groups: list[tuple[str, list[Row]]]
    chips: list[tuple[Section, int]]
    total: int


async def tasks_view(request: Request, user: User, f: str, s: str) -> TasksView:
    """Группы задач под фильтры; доступ — только через функции entries/sections."""
    db = request.app.state.db
    f = f if f in FILTERS else "all"
    menu = await make_menu(request, user)
    tasks = await list_open_tasks(db, user, menu.today)
    projects = await list_open_projects(db, user)
    think = await list_think(db, user)
    every = [*tasks, *projects, *think]
    shared = await shared_ids(db, user, every) if every else set()
    tasks = _by_filter(tasks, user, shared, f)
    projects = _by_filter(projects, user, shared, f)
    think = _by_filter(think, user, shared, f)
    visible = list(menu.section_names)
    section_id = int(s) if s.isdecimal() and int(s) in visible else None
    counts = dict.fromkeys(visible, 0)
    for e in tasks:
        if e.section_id in counts:
            counts[e.section_id] += 1
    if section_id is not None:
        tasks = [e for e in tasks if e.section_id == section_id]
        projects = [e for e in projects if e.section_id == section_id]
        think = [e for e in think if e.section_id == section_id]
    buckets: dict[str, list[Entry]] = {key: [] for key, _ in GROUPS}
    for e in tasks:
        buckets[_group_key(e, menu.today)].append(e)
    groups = [(label, buckets[key]) for key, label in GROUPS] + [
        ("Проекты", projects),
        ("Подумать", think),
    ]
    view_groups = [
        (label, await make_rows(request, menu, items)) for label, items in groups if items
    ]
    chips = [(sec, counts[sec.id]) for sec in menu.sections]
    return TasksView(menu, f, section_id, view_groups, chips, len(tasks))


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(
    request: Request,
    user: Annotated[User, Depends(current_user)],
    f: str = "all",
    s: str = "",
) -> Response:
    view = await tasks_view(request, user, f, s)
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {"user": user, "tab": "tasks", "view": view, "filters": FILTER_LABELS},
    )


@dataclass
class ConnView:
    """Подключение для показа: только имя, хост и статус словами; секрета здесь нет."""

    id: int
    name: str
    hint: str
    text: str
    dot: str  # класс цвета точки и строки статуса
    active: bool  # можно проверять и отключать


def _ago(then: str, now: datetime) -> str:
    minutes = int((now - datetime.fromisoformat(then)).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 24 * 60:
        return f"{minutes // 60} ч назад"
    return f"{minutes // (24 * 60)} дн. назад"


def conn_view(c: Connection, now: datetime) -> ConnView:
    if c.status == "ok":
        ago = _ago(c.last_success_at, now) if c.last_success_at else "—"
        text, dot = f"Работает · синхр. {ago}", "success"
    elif c.status in ("error", "auth_expired"):
        text, dot = f"Ошибка: {c.last_error or 'неизвестная ошибка'}", "error"
    elif c.status == "disabled":
        text, dot = "Отключено", "neutral"
    else:
        text, dot = "Ждёт первой синхронизации", "warning"
    return ConnView(c.id, c.name, c.hint, text, dot, c.status != "disabled")


@dataclass
class UsageRow:
    name: str
    tokens_in: int
    tokens_out: int
    pct_in: int
    pct_out: int


@dataclass
class SettingsView:
    user: User
    connections: list[ConnView]
    brief_from: str
    brief_to: str
    usage: list[UsageRow]
    limit_in: int
    limit_out: int
    shared_by_me: int
    shared_with_me: int
    users: list[User] | None  # только для админа


def _pct(used: int, limit: int) -> int:
    return min(100, used * 100 // limit) if limit > 0 else 0


async def settings_view(request: Request, user: User) -> SettingsView:
    state = request.app.state
    now = state.clock.now()
    everyone = await list_users(state.db)
    admin = user.role == "admin"
    used = await usage_for_day(state.db, state.clock.today(), None if admin else user.id)
    cfg = state.settings
    usage = [
        UsageRow(
            u.name,
            tin,
            tout,
            _pct(tin, cfg.daily_tokens_in),
            _pct(tout, cfg.daily_tokens_out),
        )
        for u in everyone
        for tin, tout in [used.get(u.id, (0, 0))]
        if admin or u.id == user.id
    ]
    by_me, with_me = await sharing_counts(state.db, user)
    return SettingsView(
        user,
        [conn_view(c, now) for c in await list_connections(state.db, user)],
        BRIEF_FROM,
        BRIEF_TO,
        usage,
        cfg.daily_tokens_in,
        cfg.daily_tokens_out,
        by_me,
        with_me,
        everyone if admin else None,
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: Annotated[User, Depends(current_user)]) -> Response:
    view = await settings_view(request, user)
    return templates.TemplateResponse(
        request, "settings.html", {"user": user, "tab": "settings", "view": view, "back": "/"}
    )


# ---------- главная-дашборд ----------

HOT_LIMIT = 5
SHARED_LIMIT = 5
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def greeting(hour: int) -> str:
    if 5 <= hour < 12:
        return "Доброе утро"
    return "День добрый" if 12 <= hour < 18 else "Добрый вечер"


def date_ru(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"


def day_label(offset: int, day: date) -> str:
    if offset == 0:
        return "Сегодня"
    if offset == 1:
        return f"Завтра, {WEEKDAYS_SHORT[day.weekday()]}"
    return WEEKDAYS[day.weekday()]


async def partners(request: Request, user: User) -> list[User]:
    return [u for u in await list_users(request.app.state.db) if u.id != user.id]


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: Annotated[User, Depends(current_user)]) -> Response:
    now = request.app.state.clock.now()
    ctx = {
        "user": user,
        "tab": "today",
        "greeting": greeting(now.hour),
        "date": date_ru(now.date()),
        "has_partner": bool(await partners(request, user)),
    }
    return templates.TemplateResponse(request, "dashboard.html", ctx)


async def _kpis(request: Request, user: User) -> dict:
    db = request.app.state.db
    today = request.app.state.clock.today()
    tasks = await list_open_tasks(db, user, today)
    projects = await list_open_projects(db, user)
    done = total = 0
    for p in projects:
        kids = await children(db, user, p.id)
        done += sum(k.status == "done" for k in kids)
        total += len(kids)
    return {
        "overdue": [t for t in tasks if t.due_date and t.due_date < today],
        "today": [t for t in tasks if t.due_date == today],
        "events": len(await events_for_day(db, user, today)),
        "projects": len(projects),
        "done": done,
        "total": total,
    }


async def _hot(request: Request, user: User) -> dict:
    db = request.app.state.db
    menu = await make_menu(request, user)
    tasks = await list_open_tasks(db, user, menu.today)
    hot = [t for t in tasks if t.due_date and t.due_date <= menu.today][:HOT_LIMIT]
    return {"rows": await make_rows(request, menu, hot), "menu": menu}


async def _calendar(request: Request, user: User) -> dict:
    state = request.app.state
    today = state.clock.today()
    days = []
    for offset in range(3):
        day = today + timedelta(days=offset)
        events = []
        for e in await events_for_day(state.db, user, day):
            when = event_time(e, day)
            events.append((when, when == ALL_DAY, e))
        days.append((day_label(offset, day), events))
    conns = await list_connections(state.db, user)
    active = [c for c in conns if c.status != "disabled"]  # отключённые не в счёт
    broken = any(c.status in ("error", "auth_expired") for c in active)
    return {"days": days, "connected": bool(active), "broken": broken}


async def _projects(request: Request, user: User) -> dict:
    db = request.app.state.db
    items = []
    for p in await list_open_projects(db, user):
        kids = await children(db, user, p.id)
        items.append((p, sum(k.status == "done" for k in kids), len(kids)))
    return {"items": items}


async def _shared(request: Request, user: User) -> dict:
    db = request.app.state.db
    others = await partners(request, user)
    if not others:
        return {"others": []}
    entries = [
        *await list_open_tasks(db, user, request.app.state.clock.today()),
        *await list_open_projects(db, user),
    ]
    shared = await shared_ids(db, user, entries) if entries else set()
    found = [e for e in entries if e.id in shared][:SHARED_LIMIT]
    title = f"{others[0].name} · общее" if len(others) == 1 else "Общее"
    return {"others": others, "title": title, "items": found}


async def _systems(request: Request, user: User) -> dict:
    state = request.app.state
    now = state.clock.now()
    return {"conns": [conn_view(c, now) for c in await list_connections(state.db, user)]}


async def _brief(request: Request, user: User) -> dict:
    state = request.app.state
    status, intro = await today_brief(state.db, user.id, state.clock.today())
    return {"intro": intro, "status": status, "tried": False}


async def _static(request: Request, user: User) -> dict:
    return {}


WIDGETS = {
    "kpis": _kpis,
    "brief": _brief,
    "calendar": _calendar,
    "hot": _hot,
    "projects": _projects,
    "shared": _shared,
    "systems": _systems,
    "mail": _static,
    "docs": _static,
}


@router.get("/widgets/{name}", response_class=HTMLResponse)
async def widget(
    request: Request, user: Annotated[User, Depends(current_user)], name: str
) -> Response:
    """Партиал виджета; модель здесь не вызывается."""
    build = WIDGETS.get(name)
    if build is None:
        raise HTTPException(status_code=404)
    data = await build(request, user)
    if name == "shared" and not data["others"]:
        return HTMLResponse("")
    return templates.TemplateResponse(request, f"partials/widgets/{name}.html", data)


def _stub(path: str, title: str, tab: str) -> None:
    @router.get(path, response_class=HTMLResponse, name=f"stub_{tab}")
    async def page(request: Request, user: Annotated[User, Depends(current_user)]) -> Response:
        return templates.TemplateResponse(
            request, "stub.html", {"user": user, "tab": tab, "title": title}
        )


_stub("/calendar", "Календарь", "calendar")
_stub("/mail", "Почта", "mail")
_stub("/docs", "Документы", "docs")
