"""Слой доступа: единственное место, где известны правила видимости, и шаринг."""

from typing import Literal

import aiosqlite

from hub.clock import Clock
from hub.models import User

ObjectType = Literal["entry", "section"]
PrincipalType = Literal["user", "space"]

_TABLES = {"entry": "entries", "section": "sections"}


def _shared_exists(object_type: str, object_id_expr: str) -> str:
    """EXISTS по shares на пользователя или его пространство; три `?` — по user_id."""
    # подставляются только константы модуля
    return (
        "EXISTS (SELECT 1 FROM shares AS sh"  # noqa: S608
        f" WHERE sh.object_type = '{object_type}' AND sh.object_id = {object_id_expr}"
        " AND ((sh.principal_type = 'user' AND sh.principal_id = ?)"
        " OR (sh.principal_type = 'space' AND sh.principal_id IN"
        " (SELECT sm.space_id FROM space_members AS sm WHERE sm.user_id = ?))))"
    )


def visible_entries_sql(user_id: int) -> tuple[str, list]:
    """Условие для `entries AS e`: своя, расшаренная или лежит в расшаренном разделе."""
    cond = (
        "(e.owner_id = ?"
        f" OR {_shared_exists('entry', 'e.id')}"
        f" OR (e.section_id IS NOT NULL AND {_shared_exists('section', 'e.section_id')}))"
    )
    return cond, [user_id] * 5


def visible_sections_sql(user_id: int) -> tuple[str, list]:
    """Условие для `sections AS s`: свой или расшаренный. Архивность не фильтруется."""
    cond = f"(s.owner_id = ? OR {_shared_exists('section', 's.id')})"
    return cond, [user_id] * 3


async def can_see_entry(db: aiosqlite.Connection, user_id: int, entry_id: int) -> bool:
    cond, params = visible_entries_sql(user_id)
    sql = f"SELECT 1 FROM entries AS e WHERE e.id = ? AND {cond}"  # noqa: S608
    row = await (await db.execute(sql, [entry_id, *params])).fetchone()
    return row is not None


async def _check_owner(
    db: aiosqlite.Connection, owner: User, object_type: ObjectType, object_id: int
) -> None:
    sql = f"SELECT owner_id FROM {_TABLES[object_type]} WHERE id = ?"  # noqa: S608
    row = await (await db.execute(sql, (object_id,))).fetchone()
    if row is None:
        raise LookupError(f"{object_type} {object_id} не найден")
    if row["owner_id"] != owner.id:
        raise PermissionError("делиться может только владелец")


async def share(
    db: aiosqlite.Connection,
    owner: User,
    object_type: ObjectType,
    object_id: int,
    principal_type: PrincipalType,
    principal_id: int,
    clock: Clock,
) -> None:
    """Открывает объект пользователю или пространству; повтор и шаринг себе — без эффекта."""
    await _check_owner(db, owner, object_type, object_id)
    table = "users" if principal_type == "user" else "spaces"
    sql = f"SELECT 1 FROM {table} WHERE id = ?"  # noqa: S608
    row = await (await db.execute(sql, (principal_id,))).fetchone()
    if row is None:
        raise LookupError(f"{principal_type} {principal_id} не найден")
    if principal_type == "user" and principal_id == owner.id:
        return
    await db.execute(
        "INSERT OR IGNORE INTO shares"
        "(object_type, object_id, principal_type, principal_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (object_type, object_id, principal_type, principal_id, clock.now().isoformat()),
    )
    await db.commit()


async def unshare(
    db: aiosqlite.Connection,
    owner: User,
    object_type: ObjectType,
    object_id: int,
    principal_type: PrincipalType,
    principal_id: int,
) -> None:
    await _check_owner(db, owner, object_type, object_id)
    await db.execute(
        "DELETE FROM shares WHERE object_type = ? AND object_id = ?"
        " AND principal_type = ? AND principal_id = ?",
        (object_type, object_id, principal_type, principal_id),
    )
    await db.commit()


async def shared_with(
    db: aiosqlite.Connection, object_type: ObjectType, object_id: int
) -> list[tuple[str, int]]:
    rows = await (
        await db.execute(
            "SELECT principal_type, principal_id FROM shares"
            " WHERE object_type = ? AND object_id = ? ORDER BY principal_type, principal_id",
            (object_type, object_id),
        )
    ).fetchall()
    return [(r["principal_type"], r["principal_id"]) for r in rows]


async def sharing_counts(db: aiosqlite.Connection, user: User) -> tuple[int, int]:
    """(сколько моих объектов открыто другим, сколько чужих открыто мне): записи и разделы."""
    mine = 0
    theirs = 0
    for object_type, table in _TABLES.items():
        # table — константа модуля
        own = await (
            await db.execute(
                f"SELECT COUNT(DISTINCT sh.object_id) FROM shares AS sh"  # noqa: S608
                f" JOIN {table} AS o ON o.id = sh.object_id"
                " WHERE sh.object_type = ? AND o.owner_id = ?",
                (object_type, user.id),
            )
        ).fetchone()
        mine += own[0]
        cond = _shared_exists(object_type, "o.id")
        other = await (
            await db.execute(
                f"SELECT COUNT(*) FROM {table} AS o WHERE o.owner_id != ? AND {cond}",  # noqa: S608
                (user.id, user.id, user.id),
            )
        ).fetchone()
        theirs += other[0]
    return mine, theirs
