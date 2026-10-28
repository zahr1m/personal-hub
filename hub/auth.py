"""Вход в веб-панель: одноразовые ссылки, сессии, initData мини-приложения."""

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import parse_qsl

import aiosqlite

from hub.clock import Clock
from hub.config import Settings
from hub.models import User
from hub.users import get_user_by_id, get_user_by_tg

LINK_TTL = timedelta(minutes=10)
LAST_SEEN_EVERY = timedelta(minutes=10)
_HASH_RE = re.compile(r"[0-9a-f]{64}")
INIT_DATA_SKEW_S = 60  # допуск на часы клиента: auth_date «из будущего»


def _hash(token: str) -> str:
    """В базе лежит только SHA-256 токена."""
    return hashlib.sha256(token.encode()).hexdigest()


async def create_login_link(db: aiosqlite.Connection, user: User, clock: Clock) -> str:
    """Сырой токен возвращается один раз; срок 10 минут."""
    token = secrets.token_urlsafe(32)
    await db.execute(
        "INSERT INTO login_links(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (_hash(token), user.id, (clock.now() + LINK_TTL).isoformat()),
    )
    await db.commit()
    return token


async def _insert_session(
    db: aiosqlite.Connection,
    user: User,
    via: Literal["link", "tma"],
    clock: Clock,
    session_days: int,
) -> str:
    """Без commit: вызывающий сам закрывает транзакцию."""
    token = secrets.token_urlsafe(32)
    now = clock.now()
    await db.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, via)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            _hash(token),
            user.id,
            now.isoformat(),
            (now + timedelta(days=session_days)).isoformat(),
            via,
        ),
    )
    return token


async def create_session(
    db: aiosqlite.Connection,
    user: User,
    via: Literal["link", "tma"],
    clock: Clock,
    session_days: int,
) -> str:
    token = await _insert_session(db, user, via, clock, session_days)
    await db.commit()
    return token


async def redeem_login_link(
    db: aiosqlite.Connection, token: str, clock: Clock, session_days: int
) -> tuple[str, User] | None:
    """None — если ссылки нет, она просрочена или уже использована."""
    h = _hash(token)
    row = await (
        await db.execute("SELECT user_id, expires_at FROM login_links WHERE token_hash = ?", (h,))
    ).fetchone()
    if row is None or clock.now() >= datetime.fromisoformat(row["expires_at"]):
        return None
    user = await get_user_by_id(db, row["user_id"])
    if user is None:
        return None
    # атомарно занимаем ссылку: проигравший гонку видит rowcount 0
    claim = await db.execute(
        "UPDATE login_links SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
        (clock.now().isoformat(), h),
    )
    if claim.rowcount != 1:
        # без rollback: соединение общее, откат стёр бы незакоммиченную заявку победителя
        return None
    try:
        session = await _insert_session(db, user, "link", clock, session_days)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return session, user


async def session_user(db: aiosqlite.Connection, token: str, clock: Clock) -> User | None:
    h = _hash(token)
    row = await (
        await db.execute(
            "SELECT user_id, expires_at, last_seen_at FROM sessions WHERE token_hash = ?", (h,)
        )
    ).fetchone()
    now = clock.now()
    if row is None or now >= datetime.fromisoformat(row["expires_at"]):
        return None
    user = await get_user_by_id(db, row["user_id"])
    if user is None:
        return None
    seen = row["last_seen_at"]
    if seen is None or now - datetime.fromisoformat(seen) >= LAST_SEEN_EVERY:
        await db.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now.isoformat(), h)
        )
        await db.commit()
    return user


async def revoke_session(db: aiosqlite.Connection, token: str, clock: Clock) -> None:
    """Запись остаётся, сессия просто истекает сейчас."""
    await db.execute(
        "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
        (clock.now().isoformat(), _hash(token)),
    )
    await db.commit()


def verify_init_data(
    init_data: str, bot_token: str, now: datetime, max_age_s: int = 3600
) -> int | None:
    """Проверка подписи initData по алгоритму Telegram; tg_id пользователя или None."""
    pairs = parse_qsl(init_data, keep_blank_values=True)
    fields = dict(pairs)
    if len(fields) != len(pairs):  # повторяющиеся ключи — подделка
        return None
    received = fields.pop("hash", None)
    if received is None or not _HASH_RE.fullmatch(received):
        return None
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), received.encode()):
        return None
    try:
        age = now.timestamp() - int(fields["auth_date"])
        if age > max_age_s or age < -INIT_DATA_SKEW_S:
            return None
        return int(json.loads(fields["user"])["id"])
    except (KeyError, ValueError, TypeError):
        return None


async def tma_login(
    db: aiosqlite.Connection, init_data: str, settings: Settings, clock: Clock
) -> tuple[str, User] | None:
    tg_id = verify_init_data(init_data, settings.bot_token, clock.now())
    if tg_id is None:
        return None
    user = await get_user_by_tg(db, tg_id)
    if user is None:
        return None
    return await create_session(db, user, "tma", clock, settings.session_days), user
