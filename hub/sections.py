"""Разделы: именованные папки записей, архивируются, но не удаляются."""

from dataclasses import dataclass

import aiosqlite

from hub.access import visible_sections_sql
from hub.clock import Clock
from hub.models import User


@dataclass
class Section:
    id: int
    owner_id: int
    name: str
    archived_at: str | None


def _section(row: aiosqlite.Row) -> Section:
    return Section(row["id"], row["owner_id"], row["name"], row["archived_at"])


async def get_or_create_section(db: aiosqlite.Connection, owner: User, name: str) -> Section:
    """Раздел владельца по имени без учёта регистра и пробелов по краям; чужие не ищем."""
    name = name.strip()
    if not name:
        raise ValueError("пустое имя раздела")
    # lower() в SQLite не знает кириллицу, поэтому сравниваем в Python
    rows = await (
        await db.execute("SELECT * FROM sections WHERE owner_id = ?", (owner.id,))
    ).fetchall()
    row = next((r for r in rows if r["name"].strip().casefold() == name.casefold()), None)
    if row is None:
        cur = await db.execute(
            "INSERT INTO sections(owner_id, name) VALUES (?, ?)", (owner.id, name)
        )
        await db.commit()
        row = await (
            await db.execute("SELECT * FROM sections WHERE id = ?", (cur.lastrowid,))
        ).fetchone()
    elif row["archived_at"] is not None:
        # имя занято архивным разделом (UNIQUE) — возвращаем его из архива
        await db.execute("UPDATE sections SET archived_at = NULL WHERE id = ?", (row["id"],))
        await db.commit()
        row = await (
            await db.execute("SELECT * FROM sections WHERE id = ?", (row["id"],))
        ).fetchone()
    return _section(row)


async def list_sections(db: aiosqlite.Connection, user: User) -> list[Section]:
    """Видимые неархивные разделы."""
    cond, params = visible_sections_sql(user.id)
    sql = f"SELECT s.* FROM sections AS s WHERE {cond} AND s.archived_at IS NULL ORDER BY s.id"  # noqa: S608
    rows = await (await db.execute(sql, params)).fetchall()
    return [_section(r) for r in rows]


async def archive_section(
    db: aiosqlite.Connection, owner: User, section_id: int, clock: Clock
) -> None:
    row = await (
        await db.execute("SELECT owner_id FROM sections WHERE id = ?", (section_id,))
    ).fetchone()
    if row is None:
        raise LookupError(f"раздел {section_id} не найден")
    if row["owner_id"] != owner.id:
        raise PermissionError("архивировать может только владелец")
    await db.execute(
        "UPDATE sections SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
        (clock.now().isoformat(), section_id),
    )
    await db.commit()
