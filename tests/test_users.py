import asyncio
from datetime import timedelta

import pytest

from hub.clock import FixedClock
from hub.users import (
    create_invite,
    ensure_admin,
    family_space_id,
    get_user_by_tg,
    list_users,
    redeem_invite,
    rename_users,
)


async def _members(db, space_id):
    rows = await (
        await db.execute("SELECT user_id FROM space_members WHERE space_id = ?", (space_id,))
    ).fetchall()
    return {r["user_id"] for r in rows}


async def test_ensure_admin_idempotent(db, clock):
    a1 = await ensure_admin(db, 100, "Аня", clock)
    a2 = await ensure_admin(db, 100, "Аня", clock)
    assert a1 == a2
    assert a1.role == "admin"
    assert len(await list_users(db)) == 1


async def test_invite_once(db, clock):
    admin = await ensure_admin(db, 100, "Аня", clock)
    code = await create_invite(db, admin, clock)
    borya = await redeem_invite(db, code, 200, "Боря", clock)
    assert borya
    assert borya.role == "user"
    assert await redeem_invite(db, code, 300, "Чужой", clock) is None
    assert await get_user_by_tg(db, 300) is None


async def test_unknown_code(db, clock):
    assert await redeem_invite(db, "nope", 200, "Боря", clock) is None


async def test_invite_expires_after_24h(db, clock):
    admin = await ensure_admin(db, 100, "Аня", clock)
    code = await create_invite(db, admin, clock)
    later = FixedClock(clock.now() + timedelta(hours=24, seconds=1))
    assert await redeem_invite(db, code, 200, "Боря", later) is None
    just_in_time = FixedClock(clock.now() + timedelta(hours=23, minutes=59))
    assert await redeem_invite(db, code, 200, "Боря", just_in_time) is not None


async def test_registered_user_does_not_spend_code(db, clock):
    admin = await ensure_admin(db, 100, "Аня", clock)
    code = await create_invite(db, admin, clock)
    again = await redeem_invite(db, code, 100, "Аня", clock)
    assert again == admin
    assert await redeem_invite(db, code, 200, "Боря", clock) is not None


async def test_only_admin_invites(db, clock):
    admin = await ensure_admin(db, 100, "Аня", clock)
    code = await create_invite(db, admin, clock)
    borya = await redeem_invite(db, code, 200, "Боря", clock)
    with pytest.raises(PermissionError):
        await create_invite(db, borya, clock)


async def test_new_user_joins_family(db, clock):
    admin = await ensure_admin(db, 100, "Аня", clock)
    code = await create_invite(db, admin, clock)
    borya = await redeem_invite(db, code, 200, "Боря", clock)
    space = await family_space_id(db)
    assert await _members(db, space) == {admin.id, borya.id}
    assert await family_space_id(db) == space


async def test_concurrent_redeem_creates_one_user(db, clock):
    admin = await ensure_admin(db, 100, "Аня", clock)
    code = await create_invite(db, admin, clock)
    results = await asyncio.gather(
        redeem_invite(db, code, 200, "Боря", clock),
        redeem_invite(db, code, 300, "Чужой", clock),
    )
    assert sum(r is not None for r in results) == 1
    assert len(await list_users(db)) == 2


async def test_names_map_beats_telegram_name(db, clock):
    admin = await ensure_admin(db, 100, "Админ", clock, names={100: "Аня"})
    assert admin.name == "Аня"
    code = await create_invite(db, admin, clock)
    user = await redeem_invite(db, code, 200, "tg-имя", clock, names={200: "Боря"})
    assert user.name == "Боря"
    other = await redeem_invite(
        db, await create_invite(db, admin, clock), 300, "Вася", clock, names={200: "Х"}
    )
    assert other.name == "Вася"


async def test_rename_users_updates_existing(db, clock):
    admin = await ensure_admin(db, 100, "Админ", clock)
    await rename_users(db, {100: "Аня", 999: "Никто"})
    assert (await get_user_by_tg(db, 100)).name == "Аня"
    assert admin.id == (await get_user_by_tg(db, 100)).id
    assert len(await list_users(db)) == 1
