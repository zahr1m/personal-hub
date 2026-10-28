import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from urllib.parse import urlencode

from hub.auth import (
    create_login_link,
    create_session,
    redeem_login_link,
    revoke_session,
    session_user,
    tma_login,
    verify_init_data,
)
from hub.clock import FixedClock

BOT = "123456:TEST-bot-token"


def make_init_data(bot_token: str, user_id: int, auth_date: datetime, **extra: str) -> str:
    """initData с настоящей подписью — тем же алгоритмом, что у Telegram."""
    fields = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "AAH-test",
        "user": json.dumps({"id": user_id, "first_name": "Аня"}, ensure_ascii=False),
        **extra,
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def later(clock, **kw):
    return FixedClock(clock.now() + timedelta(**kw))


async def test_link_single_use(db, users, clock):
    token = await create_login_link(db, users.a, clock)
    first = await redeem_login_link(db, token, clock, 30)
    assert first is not None
    assert first[1] == users.a
    assert await redeem_login_link(db, token, clock, 30) is None


async def test_link_expires_after_10_min(db, users, clock):
    token = await create_login_link(db, users.a, clock)
    assert await redeem_login_link(db, token, later(clock, minutes=11), 30) is None
    fresh = await create_login_link(db, users.a, clock)
    assert await redeem_login_link(db, fresh, later(clock, minutes=9), 30) is not None


async def test_unknown_link_none(db, users, clock):
    assert await redeem_login_link(db, "nope", clock, 30) is None


async def test_session_valid_until_30_days_then_none(db, users, clock):
    token = await create_session(db, users.b, "link", clock, 30)
    assert await session_user(db, token, later(clock, days=29)) == users.b
    assert await session_user(db, token, later(clock, days=31)) is None
    assert await session_user(db, "чужой", clock) is None


async def test_revoked_session_none(db, users, clock):
    token = await create_session(db, users.b, "tma", clock, 30)
    await revoke_session(db, token, clock)
    assert await session_user(db, token, clock) is None
    row = await (await db.execute("SELECT COUNT(*) AS n FROM sessions")).fetchone()
    assert row["n"] == 1  # запись остаётся


async def test_last_seen_updated_at_most_every_10_min(db, users, clock):
    token = await create_session(db, users.b, "link", clock, 30)

    async def seen():
        r = await (await db.execute("SELECT last_seen_at FROM sessions")).fetchone()
        return r["last_seen_at"]

    t1 = later(clock, minutes=1)
    await session_user(db, token, t1)
    assert await seen() == t1.now().isoformat()
    await session_user(db, token, later(clock, minutes=5))
    assert await seen() == t1.now().isoformat()
    t2 = later(clock, minutes=12)
    await session_user(db, token, t2)
    assert await seen() == t2.now().isoformat()


def test_init_data_valid(clock):
    now = clock.now()
    data = make_init_data(BOT, 42, now - timedelta(seconds=30), signature="sig_-x")
    assert verify_init_data(data, BOT, now) == 42


def test_init_data_wrong_bot_token(clock):
    now = clock.now()
    assert verify_init_data(make_init_data(BOT, 42, now), "999:other", now) is None


def test_init_data_tampered_field(clock):
    now = clock.now()
    data = make_init_data(BOT, 42, now)
    assert verify_init_data(data.replace("AAH-test", "AAH-evil"), BOT, now) is None
    assert verify_init_data(data.replace("%22id%22%3A+42", "%22id%22%3A+43"), BOT, now) is None


def test_init_data_too_old(clock):
    now = clock.now()
    old = make_init_data(BOT, 42, now - timedelta(seconds=3601))
    assert verify_init_data(old, BOT, now) is None
    edge = make_init_data(BOT, 42, now - timedelta(seconds=3600))
    assert verify_init_data(edge, BOT, now) == 42


def test_init_data_from_future(clock):
    now = clock.now()
    assert verify_init_data(make_init_data(BOT, 42, now + timedelta(seconds=120)), BOT, now) is None


def test_init_data_missing_hash_or_garbage(clock):
    now = clock.now()
    data = make_init_data(BOT, 42, now)
    no_hash = "&".join(p for p in data.split("&") if not p.startswith("hash="))
    assert verify_init_data(no_hash, BOT, now) is None
    assert verify_init_data("", BOT, now) is None
    assert verify_init_data("hash=abc", BOT, now) is None


async def test_tma_login_creates_tma_session(db, users, settings, clock):
    data = make_init_data(settings.bot_token, users.b.tg_id, clock.now())
    result = await tma_login(db, data, settings, clock)
    assert result is not None
    token, user = result
    assert user == users.b
    assert await session_user(db, token, clock) == users.b
    row = await (await db.execute("SELECT via FROM sessions")).fetchone()
    assert row["via"] == "tma"


async def test_tma_unknown_user_none(db, users, settings, clock):
    data = make_init_data(settings.bot_token, 777, clock.now())
    assert await tma_login(db, data, settings, clock) is None
    assert await tma_login(db, "мусор", settings, clock) is None


async def test_token_not_stored_in_plain(db, users, clock):
    link = await create_login_link(db, users.a, clock)
    sess, _ = await redeem_login_link(db, link, clock, 30)
    for table in ("login_links", "sessions"):
        rows = await (await db.execute(f"SELECT * FROM {table}")).fetchall()
        flat = " ".join(str(v) for r in rows for v in tuple(r))
        assert link not in flat
        assert sess not in flat
    row = await (await db.execute("SELECT token_hash FROM sessions")).fetchone()
    assert row["token_hash"] == hashlib.sha256(sess.encode()).hexdigest()


async def test_link_race_single_winner(db, users, clock):
    token = await create_login_link(db, users.a, clock)
    results = await asyncio.gather(
        redeem_login_link(db, token, clock, 30), redeem_login_link(db, token, clock, 30)
    )
    assert sum(r is not None for r in results) == 1
    row = await (await db.execute("SELECT COUNT(*) AS n FROM sessions")).fetchone()
    assert row["n"] == 1


def test_init_data_non_ascii_hash_none(clock):
    now = clock.now()
    data = make_init_data(BOT, 42, now)
    base = "&".join(p for p in data.split("&") if not p.startswith("hash="))
    for bad in ("%D1%91", "%FF", "ABC", "0" * 63):
        assert verify_init_data(f"{base}&hash={bad}", BOT, now) is None


def test_init_data_duplicate_keys_none(clock):
    now = clock.now()
    data = make_init_data(BOT, 42, now)
    user = next(p for p in data.split("&") if p.startswith("user="))
    hsh = next(p for p in data.split("&") if p.startswith("hash="))
    assert verify_init_data(f"{data}&{user}", BOT, now) is None
    assert verify_init_data(f"{data}&{hsh}", BOT, now) is None
