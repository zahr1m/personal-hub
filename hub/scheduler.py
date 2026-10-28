"""Расписание: утренний брифинг с повторами, сторож для админа и ночной бэкап."""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import aiosqlite
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from hub.brief import build_full
from hub.calendar_ics import sync_connection
from hub.clock import MSK, Clock
from hub.config import Settings
from hub.connections import list_connections
from hub.crypto import SecretBox
from hub.llm import Llm
from hub.models import User
from hub.users import list_users

Send = Callable[[int, str], Awaitable[None]]
Now = Callable[[], datetime]

RETRY_STEP = timedelta(minutes=15)
ATTEMPTS = 4  # первый запуск + три повтора; последний шлёт сырой список
WATCHDOG_OFFSET = timedelta(minutes=16)  # после первого повтора, чтобы видеть его итог
BACKUP_AT = (3, 30)
BACKUP_KEEP = 7
MISFIRE_GRACE = 600
DONE = ("sent", "raw")
CALENDAR_SYNC_MINUTES = 15

log = logging.getLogger(__name__)


async def _status(db: aiosqlite.Connection, user: User, day: date) -> str | None:
    row = await (
        await db.execute(
            "SELECT status FROM briefs WHERE user_id = ? AND day = ?", (user.id, day.isoformat())
        )
    ).fetchone()
    return row["status"] if row else None


async def _record(
    db: aiosqlite.Connection,
    user: User,
    day: date,
    status: str,
    error: str | None = None,
    intro: str | None = None,
) -> None:
    sent_at = datetime.now(MSK).isoformat() if status in DONE else None
    await db.execute(
        "INSERT INTO briefs(user_id, day, status, attempts, sent_at, error, intro)"
        " VALUES (?, ?, ?, 1, ?, ?, ?)"
        " ON CONFLICT(user_id, day) DO UPDATE SET status = excluded.status,"
        " attempts = attempts + 1, sent_at = excluded.sent_at, error = excluded.error,"
        " intro = excluded.intro",
        (user.id, day.isoformat(), status, sent_at, error, intro),
    )
    await db.commit()


async def run_brief(
    db: aiosqlite.Connection,
    llm: Llm,
    send: Send,
    user: User,
    day: date,
    force: bool = False,
    final_attempt: bool = False,
) -> str:
    """Итоговый статус попытки. force шлёт всегда и briefs не трогает."""
    if not force and (done := await _status(db, user, day)) in DONE:
        return done
    text, raw, intro = await build_full(db, llm, user, day)
    if force:
        await send(user.tg_id, text)
        return "raw" if raw else "sent"
    # за время build параллельный запуск мог уже отправить брифинг
    if (done := await _status(db, user, day)) in DONE:
        return done
    if raw and not final_attempt:
        await _record(db, user, day, "pending", "модель не ответила")
        return "pending"
    status = "raw" if raw else "sent"
    try:
        await send(user.tg_id, text)
    except Exception as e:  # сбой отправки не должен ронять планировщик
        await _record(db, user, day, "failed", str(e))
        return "failed"
    await _record(db, user, day, status, intro=intro)
    return status


async def watchdog(
    db: aiosqlite.Connection, send: Send, admin: User, day: date, users: list[User]
) -> None:
    """Админу — по сообщению на каждого, чей брифинг за день не ушёл."""
    for user in users:
        row = await (
            await db.execute(
                "SELECT status, error FROM briefs WHERE user_id = ? AND day = ?",
                (user.id, day.isoformat()),
            )
        ).fetchone()
        if row and row["status"] in DONE:
            continue
        reason = (row["error"] if row else None) or "нет данных"
        await send(admin.tg_id, f"брифинг для {user.name} не ушёл: {reason}")


def backup(db_path: Path, backup_dir: Path, day: date) -> None:
    """Снимок SQLite через online backup API; хранятся BACKUP_KEEP последних."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"hub-{day.isoformat()}.db"
    # только чтение: неверный путь должен падать, а не создавать пустую базу
    src = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(backup_dir.glob("hub-*.db"))[:-BACKUP_KEEP]:
        old.unlink()


def _at(brief_time: str, offset: timedelta) -> tuple[int, int]:
    hour, minute = map(int, brief_time.split(":"))
    moment = datetime(2000, 1, 1, hour, minute, tzinfo=UTC) + offset
    return moment.hour, moment.minute


def _opts() -> dict:
    return {"misfire_grace_time": MISFIRE_GRACE, "coalesce": True, "replace_existing": True}


def schedule_user(
    scheduler: AsyncIOScheduler,
    db: aiosqlite.Connection,
    llm: Llm,
    send: Send,
    user: User,
    admin: User,
    now: Now | None = None,
) -> None:
    """(Пере)создаёт задачи пользователя; id стабильны, дублей не бывает."""
    tz = scheduler.timezone
    clock = now or (lambda: datetime.now(tz))
    # до старта планировщика replace_existing не действует — чистим сами
    for job_id in [*(f"brief:{user.id}:{n}" for n in range(ATTEMPTS)), f"watchdog:{user.id}"]:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
    for n in range(ATTEMPTS):
        offset = RETRY_STEP * n
        hour, minute = _at(user.brief_time, offset)
        scheduler.add_job(
            _brief_job,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            args=[db, llm, send, user.id, offset, n == ATTEMPTS - 1, clock],
            id=f"brief:{user.id}:{n}",
            **_opts(),
        )
    hour, minute = _at(user.brief_time, WATCHDOG_OFFSET)
    scheduler.add_job(
        _watchdog_job,
        CronTrigger(hour=hour, minute=minute, timezone=tz),
        args=[db, send, admin, user.id, WATCHDOG_OFFSET, clock],
        id=f"watchdog:{user.id}",
        **_opts(),
    )


def build_scheduler(
    db: aiosqlite.Connection,
    llm: Llm,
    send: Send,
    settings: Settings,
    admin: User,
    users: list[User],
    now: Now | None = None,
) -> AsyncIOScheduler:
    tz = MSK
    scheduler = AsyncIOScheduler(timezone=tz)
    clock = now or (lambda: datetime.now(tz))
    for user in users:
        schedule_user(scheduler, db, llm, send, user, admin, clock)
    scheduler.add_job(
        _backup_job,
        CronTrigger(hour=BACKUP_AT[0], minute=BACKUP_AT[1], timezone=tz),
        args=[settings.db_path, settings.backup_dir, clock],
        id="backup",
        **_opts(),
    )
    return scheduler


def _base_day(clock: Now, offset: timedelta) -> date:
    """День базового запуска: повтор после полуночи относится к вчерашнему брифингу."""
    return (clock() - offset).date()


async def _find(db: aiosqlite.Connection, user_id: int) -> User | None:
    return next((u for u in await list_users(db) if u.id == user_id), None)


async def _brief_job(db, llm, send, user_id, offset, final_attempt, clock) -> None:
    if user := await _find(db, user_id):
        await run_brief(db, llm, send, user, _base_day(clock, offset), final_attempt=final_attempt)


async def _watchdog_job(db, send, admin, user_id, offset, clock) -> None:
    if user := await _find(db, user_id):
        await watchdog(db, send, admin, _base_day(clock, offset), [user])


async def _backup_job(db_path, backup_dir, clock) -> None:
    await asyncio.to_thread(backup, db_path, backup_dir, clock().date())


def schedule_calendar_sync(
    scheduler: AsyncIOScheduler,
    db: aiosqlite.Connection,
    box: SecretBox,
    client_factory: Callable[[], httpx.AsyncClient],
    clock: Clock,
) -> None:
    """Раз в 15 минут обновляет все включённые календари; первый раз — сразу при старте."""
    scheduler.add_job(
        _calendar_sync_job,
        IntervalTrigger(minutes=CALENDAR_SYNC_MINUTES, timezone=scheduler.timezone),
        args=[db, box, client_factory, clock],
        id="calendar_sync",
        next_run_time=clock.now(),
        **_opts(),
    )


async def _calendar_sync_job(db, box, client_factory, clock) -> None:
    """Подключения по одному; сбой одного не роняет задачу и не мешает остальным."""
    for user in await list_users(db):
        for conn in await list_connections(db, user):
            if conn.kind != "ics_calendar" or conn.status == "disabled":
                continue
            try:
                async with client_factory() as client:
                    await sync_connection(db, box, conn, client, clock)
            except Exception as exc:
                log.warning("синхронизация календаря %s: %s", conn.id, type(exc).__name__)
