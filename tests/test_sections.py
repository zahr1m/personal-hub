import pytest

from hub.access import share
from hub.sections import archive_section, get_or_create_section, list_sections


async def test_section_name_case_insensitive(db, users):
    a = await get_or_create_section(db, users.a, "отпуск ")
    b = await get_or_create_section(db, users.a, "Отпуск")
    assert a.id == b.id
    assert a.name == "отпуск"
    assert len(await list_sections(db, users.a)) == 1


async def test_same_name_different_owners(db, users):
    a = await get_or_create_section(db, users.a, "Отпуск")
    b = await get_or_create_section(db, users.b, "Отпуск")
    assert a.id != b.id


async def test_shared_section_not_found_by_name(db, users, clock):
    a = await get_or_create_section(db, users.a, "Отпуск")
    await share(db, users.a, "section", a.id, "user", users.b.id, clock)
    b = await get_or_create_section(db, users.b, "Отпуск")
    assert b.id != a.id


async def test_archived_section_hidden(db, users, clock):
    s = await get_or_create_section(db, users.a, "Отпуск")
    await archive_section(db, users.a, s.id, clock)
    assert await list_sections(db, users.a) == []


async def test_list_sections_includes_shared(db, users, clock):
    s = await get_or_create_section(db, users.a, "Отпуск")
    assert await list_sections(db, users.b) == []
    await share(db, users.a, "section", s.id, "user", users.b.id, clock)
    assert [x.id for x in await list_sections(db, users.b)] == [s.id]


async def test_archive_section_owner_only(db, users, clock):
    s = await get_or_create_section(db, users.a, "Отпуск")
    await share(db, users.a, "section", s.id, "user", users.b.id, clock)
    with pytest.raises(PermissionError):
        await archive_section(db, users.b, s.id, clock)
    with pytest.raises(LookupError):
        await archive_section(db, users.a, 999, clock)


async def test_get_or_create_unarchives(db, users, clock):
    s = await get_or_create_section(db, users.a, "Отпуск")
    await archive_section(db, users.a, s.id, clock)
    again = await get_or_create_section(db, users.a, "ОТПУСК")
    assert again.id == s.id
    assert again.archived_at is None
    assert [x.id for x in await list_sections(db, users.a)] == [s.id]
