"""Дневной бюджет токенов на пользователя."""

from datetime import date

import aiosqlite

from hub.config import Settings


class BudgetExceeded(Exception):
    """Дневной лимит токенов исчерпан."""


async def check_budget(
    db: aiosqlite.Connection, user_id: int, day: date, settings: Settings
) -> None:
    cur = await db.execute(
        "SELECT tokens_in, tokens_out FROM llm_usage WHERE user_id = ? AND day = ?",
        (user_id, day.isoformat()),
    )
    row = await cur.fetchone()
    if row and (
        row["tokens_in"] >= settings.daily_tokens_in
        or row["tokens_out"] >= settings.daily_tokens_out
    ):
        raise BudgetExceeded(f"user {user_id}: лимит токенов на {day.isoformat()} исчерпан")


async def record_usage(
    db: aiosqlite.Connection, user_id: int, day: date, tokens_in: int, tokens_out: int
) -> None:
    await db.execute(
        "INSERT INTO llm_usage(user_id, day, tokens_in, tokens_out) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, day) DO UPDATE SET "
        "tokens_in = tokens_in + excluded.tokens_in, "
        "tokens_out = tokens_out + excluded.tokens_out",
        (user_id, day.isoformat(), tokens_in, tokens_out),
    )
    await db.commit()


async def usage_for_day(
    db: aiosqlite.Connection, day: date, user_id: int | None = None
) -> dict[int, tuple[int, int]]:
    """Расход за день: user_id → (входные, выходные токены); без user_id — по всем."""
    sql = "SELECT user_id, tokens_in, tokens_out FROM llm_usage WHERE day = ?"
    params: list = [day.isoformat()]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    rows = await (await db.execute(sql, params)).fetchall()
    return {r["user_id"]: (r["tokens_in"], r["tokens_out"]) for r in rows}
