"""Пользователи, приглашения и пространство «Семья»."""

import secrets
from dataclasses import replace
from datetime import datetime, timedelta

import aiosqlite

from hub.clock import Clock
from hub.models import User

INVITE_TTL = timedelta(hours=24)
FAMILY = "Семья"
BRIEF_FROM, BRIEF_TO = "05:00", "12:00"


def _user(row: aiosqlite.Row) -> User:
    return User(row["id"], row["tg_id"], row["name"], row["role"], row["brief_time"])


async def _family_id(db: aiosqlite.Connection) -> int:
    """Без commit: вызывающий сам закрывает транзакцию."""
    await db.execute("INSERT OR IGNORE INTO spaces(name) VALUES (?)", (FAMILY,))
    row = await (await db.execute("SELECT id FROM spaces WHERE name = ?", (FAMILY,))).fetchone()
    return row["id"]


async def family_space_id(db: aiosqlite.Connection) -> int:
    space = await _family_id(db)
    await db.commit()
    return space


async def _add_user(
    db: aiosqlite.Connection, tg_id: int, name: str, role: str, clock: Clock
) -> User:
    """Пользователь + членство в «Семье»; commit — на вызывающем."""
    cur = await db.execute(
        "INSERT INTO users(tg_id, name, role, created_at) VALUES (?, ?, ?, ?)",
        (tg_id, name, role, clock.now().isoformat()),
    )
    space = await _family_id(db)
    await db.execute(
        "INSERT OR IGNORE INTO space_members(space_id, user_id) VALUES (?, ?)",
        (space, cur.lastrowid),
    )
    return User(cur.lastrowid, tg_id, name, role, "09:00")


async def get_user_by_tg(db: aiosqlite.Connection, tg_id: int) -> User | None:
    row = await (await db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))).fetchone()
    return _user(row) if row else None


async def get_user_by_id(db: aiosqlite.Connection, user_id: int) -> User | None:
    row = await (await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))).fetchone()
    return _user(row) if row else None


async def list_users(db: aiosqlite.Connection) -> list[User]:
    rows = await (await db.execute("SELECT * FROM users ORDER BY id")).fetchall()
    return [_user(r) for r in rows]


async def ensure_admin(
    db: aiosqlite.Connection,
    tg_id: int,
    name: str,
    clock: Clock,
    names: dict[int, str] | None = None,
) -> User:
    """Создаёт админа при первом запуске, повторный вызов возвращает того же."""
    if existing := await get_user_by_tg(db, tg_id):
        return existing
    user = await _add_user(db, tg_id, (names or {}).get(tg_id, name), "admin", clock)
    await db.commit()
    return user


async def create_invite(db: aiosqlite.Connection, admin: User, clock: Clock) -> str:
    if admin.role != "admin":
        raise PermissionError("приглашать может только админ")
    code = secrets.token_urlsafe(16)
    await db.execute(
        "INSERT INTO invites(code, created_by, expires_at) VALUES (?, ?, ?)",
        (code, admin.id, (clock.now() + INVITE_TTL).isoformat()),
    )
    await db.commit()
    return code


async def rename_users(db: aiosqlite.Connection, names: dict[int, str]) -> None:
    """Приводит имена уже зарегистрированных пользователей к карте tg_id → имя."""
    for tg_id, name in names.items():
        await db.execute(
            "UPDATE users SET name = ? WHERE tg_id = ? AND name != ?", (name, tg_id, name)
        )
    await db.commit()


async def redeem_invite(
    db: aiosqlite.Connection,
    code: str,
    tg_id: int,
    name: str,
    clock: Clock,
    names: dict[int, str] | None = None,
) -> User | None:
    """None — если кода нет, он просрочен или использован. Свой tg_id код не тратит."""
    if existing := await get_user_by_tg(db, tg_id):
        return existing
    row = await (
        await db.execute("SELECT expires_at FROM invites WHERE code = ?", (code,))
    ).fetchone()
    if row is None or clock.now() > datetime.fromisoformat(row["expires_at"]):
        return None
    # атомарно занимаем код: UPDATE по условию, проигравший гонку видит rowcount 0
    now = clock.now().isoformat()
    claim = await db.execute(
        "UPDATE invites SET used_at = ? WHERE code = ? AND used_at IS NULL", (now, code)
    )
    if claim.rowcount != 1:
        return None
    try:
        user = await _add_user(db, tg_id, (names or {}).get(tg_id, name), "user", clock)
        await db.execute("UPDATE invites SET used_by = ? WHERE code = ?", (user.id, code))
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return user


async def set_brief_time(db: aiosqlite.Connection, user: User, value: str) -> User:
    """Время утреннего брифинга «ЧЧ:ММ» в пределах 05:00–12:00, иначе ValueError."""
    if len(value) != 5 or value[2] != ":" or not (value[:2] + value[3:]).isdecimal():
        raise ValueError("время нужно в формате ЧЧ:ММ")
    hour, minute = int(value[:2]), int(value[3:])
    if hour > 23 or minute > 59 or not BRIEF_FROM <= value <= BRIEF_TO:
        raise ValueError(f"время брифинга — от {BRIEF_FROM} до {BRIEF_TO}")
    await db.execute("UPDATE users SET brief_time = ? WHERE id = ?", (value, user.id))
    await db.commit()
    return replace(user, brief_time=value)
