"""Утренний бриф: факты из базы собирает код, модель пишет только вступление."""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import aiosqlite

from hub.budget import BudgetExceeded
from hub.calendar_ics import Event, events_for_day
from hub.clock import MSK
from hub.entries import Entry, list_open_tasks, list_think, shared_ids
from hub.llm import Llm, LlmError
from hub.models import User

UPCOMING_DAYS = 7
THINK_LIMIT = 2
TG_LIMIT = 4096
EMPTY_DAY = "Задач на сегодня нет"
BULLET, OVERDUE = "• ", "⚠️ "
ALL_DAY = "весь день"
SECTIONS = ("Календарь", "Горит", "На неделе", "Подумать")
# пункты, которые можно выбросить при обрезке: задачи и строки календаря
DROPPABLE = re.compile(
    rf"^({re.escape(BULLET)}|{re.escape(OVERDUE)}|\d\d:\d\d |до \d\d:\d\d |{ALL_DAY} · )"
)


@dataclass
class BriefFacts:
    day: date
    overdue: list[Entry]
    today: list[Entry]
    upcoming: list[Entry]
    think: list[Entry]
    shared: set[int]
    events: list[Event] = field(default_factory=list)


def event_time(event: Event, day: date) -> str:
    """«HH:MM» начала, «до HH:MM» для перешедшего через полночь или «весь день»."""
    start = event.starts_at.astimezone(MSK)
    if event.all_day:
        return ALL_DAY
    if start.date() < day:  # началось раньше: если сегодня кончается — «до HH:MM»
        end = event.ends_at.astimezone(MSK) if event.ends_at else None
        return f"до {end:%H:%M}" if end and end.date() == day else ALL_DAY
    return f"{start:%H:%M}"


async def collect(db: aiosqlite.Connection, user: User, day: date) -> BriefFacts:
    tasks = await list_open_tasks(db, user, day)
    horizon = day + timedelta(days=UPCOMING_DAYS)
    overdue = [t for t in tasks if t.due_date and t.due_date < day]
    today = [t for t in tasks if t.due_date == day]
    upcoming = [t for t in tasks if t.due_date and day < t.due_date <= horizon]
    # мысли — раз в неделю, по понедельникам
    think = (await list_think(db, user))[:THINK_LIMIT] if day.weekday() == 0 else []
    shared = await shared_ids(db, user, [*overdue, *today, *upcoming, *think])
    events = await events_for_day(db, user, day)
    return BriefFacts(day, overdue, today, upcoming, think, shared, events)


def render_lists(facts: BriefFacts) -> str:
    def line(e: Entry, prefix: str = BULLET) -> str:
        text = f"{prefix}{e.title}"
        if e.due_date:
            text += f" · {e.due_date:%d.%m}"
        if e.id in facts.shared:
            text += " 👥"
        return text

    def event_line(e: Event) -> str:
        when = event_time(e, facts.day)
        return f"{when} · {e.title}" if when == ALL_DAY else f"{when} {e.title}"

    burning = [line(e, OVERDUE) for e in facts.overdue] + [line(e) for e in facts.today]
    sections = [
        ("Календарь", [event_line(e) for e in facts.events]),
        ("Горит", burning),
        ("На неделе", [line(e) for e in facts.upcoming]),
        ("Подумать", [line(e) for e in facts.think]),
    ]
    blocks = [f"{title}\n" + "\n".join(lines) for title, lines in sections if lines]
    return "\n\n".join(blocks) if blocks else EMPTY_DAY


def _truncate(text: str, limit: int = TG_LIMIT) -> str:
    """Режет по целым строкам-пунктам с конца и дописывает, сколько пунктов убрано."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    dropped = 0
    while True:
        footer = f"…ещё {dropped} пунктов — /tasks"
        if len("\n".join([*lines, footer])) <= limit:
            break
        idx = next(
            (i for i in range(len(lines) - 1, -1, -1) if DROPPABLE.match(lines[i])),
            None,
        )
        if idx is None:
            break
        del lines[idx]
        dropped += 1
    while lines and (not lines[-1] or lines[-1] in SECTIONS):
        lines.pop()  # не оставляем пустые строки и заголовки без пунктов
    return "\n".join([*lines, f"…ещё {dropped} пунктов — /tasks"])


async def build_full(
    db: aiosqlite.Connection, llm: Llm, user: User, day: date
) -> tuple[str, bool, str | None]:
    """(текст, raw, вступление): raw=True, если вступление не получилось (тогда оно None)."""
    lists = render_lists(await collect(db, user, day))
    try:
        intro = (await llm.brief_intro(user.id, lists)).strip()
    except (LlmError, BudgetExceeded):
        return _truncate(lists), True, None
    # режем только списки: строки вступления выбрасывать нельзя
    return f"{intro}\n\n{_truncate(lists, max(0, TG_LIMIT - len(intro) - 2))}", False, intro


async def build(db: aiosqlite.Connection, llm: Llm, user: User, day: date) -> tuple[str, bool]:
    """(текст, raw): raw=True, если вступление не получилось и отдан только список."""
    text, raw, _ = await build_full(db, llm, user, day)
    return text, raw


async def today_brief(
    db: aiosqlite.Connection, user_id: int, day: date
) -> tuple[str | None, str | None]:
    """(статус, вступление) за день: утреннее из briefs, иначе собранное кнопкой."""
    args = (user_id, day.isoformat())
    row = await (
        await db.execute("SELECT status, intro FROM briefs WHERE user_id = ? AND day = ?", args)
    ).fetchone()
    status, intro = (row["status"], row["intro"]) if row else (None, None)
    if intro is None:
        manual = await (
            await db.execute("SELECT intro FROM brief_manual WHERE user_id = ? AND day = ?", args)
        ).fetchone()
        intro = manual["intro"] if manual else None
    return status, intro


async def save_manual_intro(
    db: aiosqlite.Connection, user_id: int, day: date, intro: str, created_at: str
) -> None:
    await db.execute(
        "INSERT INTO brief_manual(user_id, day, intro, created_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(user_id, day) DO UPDATE SET intro = excluded.intro,"
        " created_at = excluded.created_at",
        (user_id, day.isoformat(), intro, created_at),
    )
    await db.commit()
