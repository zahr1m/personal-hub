import pytest

from hub.access import (
    can_see_entry,
    share,
    shared_with,
    unshare,
    visible_entries_sql,
    visible_sections_sql,
)
from hub.users import family_space_id

NOW = "2026-11-02T08:00:00+03:00"


async def add_entry(db, owner, section_id=None):
    cur = await db.execute(
        "INSERT INTO entries(owner_id, kind, title, section_id, created_at)"
        " VALUES (?, 'note', 'x', ?, ?)",
        (owner.id, section_id, NOW),
    )
    await db.commit()
    return cur.lastrowid


async def add_section(db, owner, name="Отпуск"):
    cur = await db.execute("INSERT INTO sections(owner_id, name) VALUES (?, ?)", (owner.id, name))
    await db.commit()
    return cur.lastrowid


async def visible_ids(db, user):
    cond, params = visible_entries_sql(user.id)
    sql = f"SELECT e.id FROM entries AS e WHERE {cond}"
    rows = await (await db.execute(sql, params)).fetchall()
    return {r["id"] for r in rows}


async def test_private_by_default(db, users):
    entry = await add_entry(db, users.a)
    assert await can_see_entry(db, users.a.id, entry)
    assert not await can_see_entry(db, users.b.id, entry)


async def test_nonexistent_entry_not_visible(db, users):
    assert not await can_see_entry(db, users.a.id, 999)


async def test_share_to_user(db, users, clock):
    entry = await add_entry(db, users.a)
    await share(db, users.a, "entry", entry, "user", users.b.id, clock)
    assert await can_see_entry(db, users.b.id, entry)
    assert not await can_see_entry(db, users.c.id, entry)


async def test_share_to_family_space(db, users, clock):
    entry = await add_entry(db, users.a)
    family = await family_space_id(db)
    await share(db, users.a, "entry", entry, "space", family, clock)
    assert await can_see_entry(db, users.b.id, entry)
    assert not await can_see_entry(db, users.c.id, entry)


async def test_shared_section_opens_its_entries(db, users, clock):
    section = await add_section(db, users.a)
    in_section = await add_entry(db, users.a, section)
    outside = await add_entry(db, users.a)
    await share(db, users.a, "section", section, "user", users.b.id, clock)
    assert await can_see_entry(db, users.b.id, in_section)
    assert not await can_see_entry(db, users.b.id, outside)
    assert not await can_see_entry(db, users.c.id, in_section)


async def test_visible_sections(db, users, clock):
    section = await add_section(db, users.a)
    other = await add_section(db, users.a, "Дом")
    family = await family_space_id(db)
    await share(db, users.a, "section", section, "space", family, clock)

    async def ids(user):
        cond, params = visible_sections_sql(user.id)
        sql = f"SELECT s.id FROM sections AS s WHERE {cond}"
        rows = await (await db.execute(sql, params)).fetchall()
        return {r["id"] for r in rows}

    assert await ids(users.a) == {section, other}
    assert await ids(users.b) == {section}
    assert await ids(users.c) == set()


async def test_visible_entries_sql_lists_exactly_visible(db, users, clock):
    own = await add_entry(db, users.b)
    shared = await add_entry(db, users.a)
    await add_entry(db, users.a)
    await share(db, users.a, "entry", shared, "user", users.b.id, clock)
    assert await visible_ids(db, users.b) == {own, shared}


async def test_multiple_shares_do_not_duplicate_rows(db, users, clock):
    section = await add_section(db, users.a)
    entry = await add_entry(db, users.a, section)
    family = await family_space_id(db)
    await share(db, users.a, "entry", entry, "user", users.b.id, clock)
    await share(db, users.a, "entry", entry, "space", family, clock)
    await share(db, users.a, "section", section, "user", users.b.id, clock)
    cond, params = visible_entries_sql(users.b.id)
    sql = f"SELECT e.id FROM entries AS e WHERE {cond}"
    rows = await (await db.execute(sql, params)).fetchall()
    assert [r["id"] for r in rows] == [entry]


async def test_only_owner_shares(db, users, clock):
    entry = await add_entry(db, users.a)
    section = await add_section(db, users.a)
    with pytest.raises(PermissionError):
        await share(db, users.b, "entry", entry, "user", users.c.id, clock)
    with pytest.raises(PermissionError):
        await share(db, users.b, "section", section, "user", users.c.id, clock)
    assert not await can_see_entry(db, users.c.id, entry)


async def test_share_unknown_objects(db, users, clock):
    entry = await add_entry(db, users.a)
    with pytest.raises(LookupError):
        await share(db, users.a, "entry", 999, "user", users.b.id, clock)
    with pytest.raises(LookupError):
        await share(db, users.a, "entry", entry, "user", 999, clock)
    with pytest.raises(LookupError):
        await share(db, users.a, "entry", entry, "space", 999, clock)


async def test_share_is_idempotent_and_self_share_is_noop(db, users, clock):
    entry = await add_entry(db, users.a)
    await share(db, users.a, "entry", entry, "user", users.b.id, clock)
    await share(db, users.a, "entry", entry, "user", users.b.id, clock)
    await share(db, users.a, "entry", entry, "user", users.a.id, clock)
    assert await shared_with(db, "entry", entry) == [("user", users.b.id)]


async def test_unshare_closes_access(db, users, clock):
    entry = await add_entry(db, users.a)
    await share(db, users.a, "entry", entry, "user", users.b.id, clock)
    await unshare(db, users.a, "entry", entry, "user", users.b.id)
    assert not await can_see_entry(db, users.b.id, entry)
    await unshare(db, users.a, "entry", entry, "user", users.b.id)  # повтор без ошибки


async def test_only_owner_unshares(db, users, clock):
    entry = await add_entry(db, users.a)
    await share(db, users.a, "entry", entry, "user", users.b.id, clock)
    with pytest.raises(PermissionError):
        await unshare(db, users.b, "entry", entry, "user", users.b.id)
    assert await can_see_entry(db, users.b.id, entry)


async def visible_section_ids(db, user):
    cond, params = visible_sections_sql(user.id)
    sql = f"SELECT s.id FROM sections AS s WHERE {cond}"
    rows = await (await db.execute(sql, params)).fetchall()
    return {r["id"] for r in rows}


async def test_shared_section_does_not_expose_entry_with_same_id(db, users, clock):
    """Регрессия: id записи и id раздела — разные пространства имён."""
    section = await add_section(db, users.a, "S1")  # section id 1
    entry = await add_entry(db, users.a)  # entry id 1 вне разделов
    assert section == entry
    await share(db, users.a, "section", section, "user", users.b.id, clock)
    assert entry not in await visible_ids(db, users.b)
    assert not await can_see_entry(db, users.b.id, entry)


async def test_shared_entry_does_not_expose_section_with_same_id(db, users, clock):
    await add_section(db, users.a, "S1")  # section id 1
    section = await add_section(db, users.a, "S2")  # section id 2
    await add_entry(db, users.a)  # entry id 1
    entry = await add_entry(db, users.a, section)  # entry id 2 в разделе S2
    assert section == entry
    await share(db, users.a, "entry", entry, "user", users.b.id, clock)
    assert section not in await visible_section_ids(db, users.b)
    from hub.sections import list_sections

    assert [s.id for s in await list_sections(db, users.b)] == []
