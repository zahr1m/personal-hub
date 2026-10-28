"""Записи: задачи, проекты, размышления, заметки."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import aiosqlite

from hub.access import can_see_entry, visible_entries_sql, visible_sections_sql
from hub.clock import Clock
from hub.models import EntryKind, EntryStatus, User

CONTOURS = ("личное", "работа", "хобби", "учёба", "финансы", "быт")


def resolve_due(raw: date | None, today: date) -> date | None:
    """Срок от модели: вчера и позже — как есть; старше — тот же день в этом году, иначе None."""
    if raw is None or raw >= today - timedelta(days=1):
        return raw
    try:
        shifted = raw.replace(year=today.year)
    except ValueError:  # 29 февраля в невисокосном году
        return None
    return shifted if shifted >= today else None


@dataclass
class Entry:
    id: int
    owner_id: int
    kind: EntryKind
    title: str
    body: str
    due_date: date | None
    contour: str | None
    status: EntryStatus
    parent_id: int | None
    section_id: int | None
    created_at: datetime
    done_at: datetime | None


def _entry(row: aiosqlite.Row) -> Entry:
    return Entry(
        row["id"],
        row["owner_id"],
        row["kind"],
        row["title"],
        row["body"],
        date.fromisoformat(row["due_date"]) if row["due_date"] else None,
        row["contour"],
        row["status"],
        row["parent_id"],
        row["section_id"],
        datetime.fromisoformat(row["created_at"]),
        datetime.fromisoformat(row["done_at"]) if row["done_at"] else None,
    )


async def _get(db: aiosqlite.Connection, entry_id: int) -> Entry:
    row = await (await db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,))).fetchone()
    if row is None:
        raise LookupError(f"запись {entry_id} не найдена")
    return _entry(row)


async def get_entry(db: aiosqlite.Connection, user: User, entry_id: int) -> Entry:
    """Запись, если пользователь её видит; иначе PermissionError (и для несуществующей тоже)."""
    if not await can_see_entry(db, user.id, entry_id):
        raise PermissionError("запись недоступна")
    return await _get(db, entry_id)


async def _check_section_visible(db: aiosqlite.Connection, user: User, section_id: int) -> None:
    cond, params = visible_sections_sql(user.id)
    sql = f"SELECT 1 FROM sections AS s WHERE s.id = ? AND {cond}"  # noqa: S608
    if await (await db.execute(sql, [section_id, *params])).fetchone() is None:
        raise PermissionError("раздел недоступен")


async def _owned(db: aiosqlite.Connection, user: User, entry_id: int) -> Entry:
    entry = await _get(db, entry_id)
    if entry.owner_id != user.id:
        raise PermissionError("менять может только владелец")
    return entry


async def create_entry(
    db: aiosqlite.Connection,
    owner: User,
    kind: EntryKind,
    title: str,
    body: str = "",
    due_date: date | None = None,
    contour: str | None = None,
    section_id: int | None = None,
    parent_id: int | None = None,
    *,
    clock: Clock,
) -> Entry:
    title = title.strip()
    if not title:
        raise ValueError("пустой заголовок")
    if section_id is not None:
        await _check_section_visible(db, owner, section_id)
    if parent_id is not None and not await can_see_entry(db, owner.id, parent_id):
        raise PermissionError("родительская запись недоступна")
    if contour not in CONTOURS:
        contour = None
    cur = await db.execute(
        "INSERT INTO entries(owner_id, kind, title, body, due_date, contour,"
        " parent_id, section_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            owner.id,
            kind,
            title,
            body,
            due_date.isoformat() if due_date else None,
            contour,
            parent_id,
            section_id,
            clock.now().isoformat(),
        ),
    )
    await db.commit()
    return await _get(db, cur.lastrowid)


async def set_kind(db: aiosqlite.Connection, user: User, entry_id: int, kind: EntryKind) -> Entry:
    await _owned(db, user, entry_id)
    await db.execute("UPDATE entries SET kind = ? WHERE id = ?", (kind, entry_id))
    await db.commit()
    return await _get(db, entry_id)


async def set_section(
    db: aiosqlite.Connection, user: User, entry_id: int, section_id: int | None
) -> Entry:
    await _owned(db, user, entry_id)
    if section_id is not None:
        await _check_section_visible(db, user, section_id)
    await db.execute("UPDATE entries SET section_id = ? WHERE id = ?", (section_id, entry_id))
    await db.commit()
    return await _get(db, entry_id)


async def complete(db: aiosqlite.Connection, user: User, entry_id: int, clock: Clock) -> Entry:
    """Закрывает запись; доступно любому, кто её видит. Повтор не трогает done_at."""
    if not await can_see_entry(db, user.id, entry_id):
        raise PermissionError("запись недоступна")
    await db.execute(
        "UPDATE entries SET status = 'done', done_at = ? WHERE id = ? AND status = 'open'",
        (clock.now().isoformat(), entry_id),
    )
    await db.commit()
    return await _get(db, entry_id)


async def drop(db: aiosqlite.Connection, user: User, entry_id: int, clock: Clock) -> Entry:
    """Отменяет открытую запись; только владелец. Повтор ничего не меняет."""
    await _owned(db, user, entry_id)
    await db.execute(
        "UPDATE entries SET status = 'dropped', done_at = ? WHERE id = ? AND status = 'open'",
        (clock.now().isoformat(), entry_id),
    )
    await db.commit()
    return await _get(db, entry_id)


async def reopen(db: aiosqlite.Connection, user: User, entry_id: int) -> Entry:
    """Возвращает в работу выполненную или отменённую запись; доступно любому, кто её видит."""
    if not await can_see_entry(db, user.id, entry_id):
        raise PermissionError("запись недоступна")
    await db.execute(
        "UPDATE entries SET status = 'open', done_at = NULL WHERE id = ? AND status != 'open'",
        (entry_id,),
    )
    await db.commit()
    return await _get(db, entry_id)


async def _select(
    db: aiosqlite.Connection, user: User, where: str, args: list, order: str
) -> list[Entry]:
    cond, params = visible_entries_sql(user.id)
    # where/order — константы модуля, значения идут параметрами
    sql = f"SELECT e.* FROM entries AS e WHERE {cond} AND {where} ORDER BY {order}"  # noqa: S608
    rows = await (await db.execute(sql, [*params, *args])).fetchall()
    return [_entry(r) for r in rows]


async def list_open_tasks(db: aiosqlite.Connection, user: User, today: date) -> list[Entry]:
    """Просроченные, сегодня, дальше по дате, без даты; при равенстве — старые первыми."""
    iso = today.isoformat()
    order = (
        "CASE WHEN e.due_date IS NULL THEN 3 WHEN e.due_date < ? THEN 0"
        " WHEN e.due_date = ? THEN 1 ELSE 2 END, e.due_date, e.created_at, e.id"
    )
    cond, params = visible_entries_sql(user.id)
    sql = (
        f"SELECT e.* FROM entries AS e WHERE {cond}"  # noqa: S608
        f" AND e.kind = 'task' AND e.status = 'open' ORDER BY {order}"
    )
    rows = await (await db.execute(sql, [*params, iso, iso])).fetchall()
    return [_entry(r) for r in rows]


async def list_open_projects(db: aiosqlite.Connection, user: User) -> list[Entry]:
    return await _select(
        db, user, "e.kind = 'project' AND e.status = 'open'", [], "e.created_at, e.id"
    )


async def list_think(db: aiosqlite.Connection, user: User) -> list[Entry]:
    return await _select(
        db, user, "e.kind = 'think' AND e.status = 'open'", [], "e.created_at, e.id"
    )


async def children(db: aiosqlite.Connection, user: User, parent_id: int) -> list[Entry]:
    return await _select(db, user, "e.parent_id = ?", [parent_id], "e.created_at, e.id")


async def shared_ids(db: aiosqlite.Connection, viewer: User, entries: list[Entry]) -> set[int]:
    """id записей для метки 👥: чужие или открытые кому-то ещё (напрямую или через раздел)."""
    ids = [e.id for e in entries]
    sections = list({e.section_id for e in entries if e.section_id is not None})
    sql = (  # плейсхолдеры — только «?», значения идут параметрами
        "SELECT DISTINCT object_type, object_id FROM shares WHERE "  # noqa: S608
        f"(object_type = 'entry' AND object_id IN ({','.join('?' * len(ids))}))"
        f" OR (object_type = 'section' AND object_id IN ({','.join('?' * len(sections))}))"
    )
    rows = await (await db.execute(sql, [*ids, *sections])).fetchall()
    entry_ids = {r["object_id"] for r in rows if r["object_type"] == "entry"}
    section_ids = {r["object_id"] for r in rows if r["object_type"] == "section"}
    return {
        e.id
        for e in entries
        if e.owner_id != viewer.id or e.id in entry_ids or e.section_id in section_ids
    }
