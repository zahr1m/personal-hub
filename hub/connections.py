"""Подключения пользователя (ссылки iCal): секрет хранится только зашифрованным."""

from dataclasses import dataclass

import aiosqlite

from hub.clock import Clock
from hub.crypto import SecretBox
from hub.models import User


@dataclass(frozen=True)
class Connection:
    id: int
    owner_id: int
    kind: str
    name: str
    hint: str
    status: str
    last_sync_at: str | None
    last_success_at: str | None
    last_error: str | None


_FIELDS = (
    "id",
    "owner_id",
    "kind",
    "name",
    "hint",
    "status",
    "last_sync_at",
    "last_success_at",
    "last_error",
)


def _conn(row: aiosqlite.Row) -> Connection:
    return Connection(*(row[k] for k in _FIELDS))


async def add_connection(
    db: aiosqlite.Connection,
    box: SecretBox,
    owner: User,
    kind: str,
    name: str,
    secret: str,
    hint: str,
    clock: Clock,
) -> Connection:
    cur = await db.execute(
        "INSERT INTO connections(owner_id, kind, name, secret_enc, hint, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (owner.id, kind, name, box.seal(secret), hint, clock.now().isoformat()),
    )
    await db.commit()
    row = await (
        await db.execute(
            "SELECT id, owner_id, kind, name, hint, status, last_sync_at, last_success_at,"
            " last_error FROM connections WHERE id = ?",
            (cur.lastrowid,),
        )
    ).fetchone()
    return _conn(row)


async def list_connections(db: aiosqlite.Connection, user: User) -> list[Connection]:
    cur = await db.execute(
        "SELECT id, owner_id, kind, name, hint, status, last_sync_at, last_success_at,"
        " last_error FROM connections WHERE owner_id = ? ORDER BY id",
        (user.id,),
    )
    return [_conn(r) for r in await cur.fetchall()]


async def _own_row(db: aiosqlite.Connection, user: User, connection_id: int) -> aiosqlite.Row:
    row = await (
        await db.execute(
            "SELECT owner_id, secret_enc FROM connections WHERE id = ?", (connection_id,)
        )
    ).fetchone()
    if row is None or row["owner_id"] != user.id:
        raise PermissionError("подключение недоступно")
    return row


async def get_secret(
    db: aiosqlite.Connection, box: SecretBox, user: User, connection_id: int
) -> str:
    row = await _own_row(db, user, connection_id)
    blob = bytes(row["secret_enc"])
    return box.open(blob) if blob else ""  # пусто — подключение отключено


async def set_status(
    db: aiosqlite.Connection,
    connection_id: int,
    status: str,
    error: str | None,
    clock: Clock,
    success: bool,
) -> None:
    now = clock.now().isoformat()
    await db.execute(
        "UPDATE connections SET status = ?, last_error = ?, last_sync_at = ?,"
        " last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END"
        " WHERE id = ? AND status != 'disabled'",
        (status, error, now, success, now, connection_id),
    )
    await db.commit()


async def disable_connection(
    db: aiosqlite.Connection, user: User, connection_id: int, clock: Clock
) -> None:
    """Не удаление: запись остаётся, секрет затирается (пустой blob, ключ не нужен)."""
    await _own_row(db, user, connection_id)
    await db.execute(
        "UPDATE connections SET status = 'disabled', secret_enc = ?, last_sync_at = ? WHERE id = ?",
        (b"", clock.now().isoformat(), connection_id),
    )
    await db.commit()
