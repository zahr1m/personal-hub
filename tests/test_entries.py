from datetime import date, timedelta

import pytest

from hub.access import share
from hub.entries import (
    children,
    complete,
    create_entry,
    drop,
    list_open_tasks,
    list_think,
    reopen,
    set_kind,
    set_section,
)
from hub.sections import get_or_create_section

TODAY = date(2026, 11, 2)


async def task(db, user, clock, title="t", due=None, **kw):
    return await create_entry(db, user, "task", title, due_date=due, clock=clock, **kw)


async def test_list_open_tasks_order(db, users, clock):
    none = await task(db, users.a, clock, "без даты")
    later = await task(db, users.a, clock, "позже", TODAY + timedelta(days=3))
    today = await task(db, users.a, clock, "сегодня", TODAY)
    old = await task(db, users.a, clock, "просрочено", TODAY - timedelta(days=2))
    older = await task(db, users.a, clock, "ещё раньше", TODAY - timedelta(days=5))
    got = await list_open_tasks(db, users.a, TODAY)
    assert [e.id for e in got] == [older.id, old.id, today.id, later.id, none.id]


async def test_list_open_tasks_ties_by_created_at(db, users, clock):
    first = await task(db, users.a, clock, "первая", TODAY)
    clock._dt += timedelta(minutes=1)
    second = await task(db, users.a, clock, "вторая", TODAY)
    got = await list_open_tasks(db, users.a, TODAY)
    assert [e.id for e in got] == [first.id, second.id]


async def test_list_open_tasks_only_open_visible_tasks(db, users, clock):
    mine = await task(db, users.a, clock)
    done = await task(db, users.a, clock)
    await complete(db, users.a, done.id, clock)
    await create_entry(db, users.a, "note", "заметка", clock=clock)
    await task(db, users.b, clock, "чужая")
    got = await list_open_tasks(db, users.a, TODAY)
    assert [e.id for e in got] == [mine.id]


async def test_complete_twice_keeps_done_at(db, users, clock):
    t = await task(db, users.a, clock)
    first = await complete(db, users.a, t.id, clock)
    assert first.status == "done"
    assert first.done_at == clock.now()
    clock._dt += timedelta(hours=1)
    second = await complete(db, users.a, t.id, clock)
    assert second.done_at == first.done_at


async def test_complete_shared_task_by_member(db, users, clock):
    t = await task(db, users.a, clock)
    await share(db, users.a, "entry", t.id, "user", users.b.id, clock)
    done = await complete(db, users.b, t.id, clock)
    assert done.status == "done"


async def test_complete_hidden_task_denied(db, users, clock):
    t = await task(db, users.a, clock)
    with pytest.raises(PermissionError):
        await complete(db, users.c, t.id, clock)
    assert (await list_open_tasks(db, users.a, TODAY))[0].status == "open"


async def test_complete_missing_entry(db, users, clock):
    with pytest.raises(PermissionError):
        await complete(db, users.a, 999, clock)


async def test_subtasks_listed_under_project(db, users, clock):
    p = await create_entry(db, users.a, "project", "Ремонт", clock=clock)
    s1 = await task(db, users.a, clock, "один", parent_id=p.id)
    s2 = await task(db, users.a, clock, "два", parent_id=p.id)
    await task(db, users.a, clock, "посторонняя")
    got = await children(db, users.a, p.id)
    assert [e.id for e in got] == [s1.id, s2.id]
    assert all(e.parent_id == p.id for e in got)
    assert await children(db, users.c, p.id) == []


async def test_parent_must_be_visible(db, users, clock):
    p = await create_entry(db, users.a, "project", "Ремонт", clock=clock)
    with pytest.raises(PermissionError):
        await task(db, users.c, clock, parent_id=p.id)


async def test_section_must_be_visible(db, users, clock):
    s = await get_or_create_section(db, users.a, "Отпуск")
    with pytest.raises(PermissionError):
        await task(db, users.b, clock, section_id=s.id)
    await share(db, users.a, "section", s.id, "user", users.b.id, clock)
    e = await task(db, users.b, clock, section_id=s.id)
    assert e.section_id == s.id


async def test_unknown_contour_becomes_none(db, users, clock):
    e = await task(db, users.a, clock, contour="выдуманный")
    assert e.contour is None
    k = await task(db, users.a, clock, contour="работа")
    assert k.contour == "работа"


async def test_title_trimmed_and_empty_rejected(db, users, clock):
    e = await task(db, users.a, clock, "  купить хлеб ")
    assert e.title == "купить хлеб"
    with pytest.raises(ValueError, match="пустой"):
        await task(db, users.a, clock, "   ")


async def test_dates_stored_iso(db, users, clock):
    e = await task(db, users.a, clock, due=TODAY)
    assert e.due_date == TODAY
    assert e.created_at == clock.now()
    row = await (await db.execute("SELECT due_date, created_at FROM entries")).fetchone()
    assert row["due_date"] == "2026-11-02"
    assert row["created_at"] == "2026-11-02T08:00:00+03:00"


async def test_list_think_oldest_first(db, users, clock):
    t1 = await create_entry(db, users.a, "think", "первая", clock=clock)
    clock._dt += timedelta(minutes=1)
    t2 = await create_entry(db, users.a, "think", "вторая", clock=clock)
    await task(db, users.a, clock)
    got = await list_think(db, users.a)
    assert [e.id for e in got] == [t1.id, t2.id]
    assert await list_think(db, users.c) == []


async def test_set_kind_owner_only(db, users, clock):
    e = await create_entry(db, users.a, "note", "x", clock=clock)
    changed = await set_kind(db, users.a, e.id, "task")
    assert changed.kind == "task"
    await share(db, users.a, "entry", e.id, "user", users.b.id, clock)
    with pytest.raises(PermissionError):
        await set_kind(db, users.b, e.id, "think")
    with pytest.raises(LookupError):
        await set_kind(db, users.a, 999, "think")


async def test_set_section(db, users, clock):
    s = await get_or_create_section(db, users.a, "Отпуск")
    e = await task(db, users.a, clock)
    assert (await set_section(db, users.a, e.id, s.id)).section_id == s.id
    assert (await set_section(db, users.a, e.id, None)).section_id is None
    with pytest.raises(PermissionError):
        await set_section(db, users.b, e.id, None)
    other = await get_or_create_section(db, users.b, "Чужой")
    with pytest.raises(PermissionError):
        await set_section(db, users.a, e.id, other.id)


async def test_children_hides_private_subtask_of_shared_project(db, users, clock):
    p = await create_entry(db, users.a, "project", "Ремонт", clock=clock)
    await share(db, users.a, "entry", p.id, "user", users.b.id, clock)
    sub = await task(db, users.a, clock, "личная подзадача", parent_id=p.id)
    assert await children(db, users.b, p.id) == []
    assert [e.id for e in await children(db, users.a, p.id)] == [sub.id]


async def test_drop_owner_only_and_idempotent(db, users, clock):
    t = await task(db, users.a, clock)
    await share(db, users.a, "entry", t.id, "user", users.b.id, clock)
    with pytest.raises(PermissionError):
        await drop(db, users.b, t.id, clock)
    first = await drop(db, users.a, t.id, clock)
    assert first.status == "dropped"
    clock._dt += timedelta(hours=1)
    assert (await drop(db, users.a, t.id, clock)).done_at == first.done_at


async def test_complete_does_not_touch_dropped(db, users, clock):
    t = await task(db, users.a, clock)
    await drop(db, users.a, t.id, clock)
    assert (await complete(db, users.a, t.id, clock)).status == "dropped"


async def test_drop_does_not_touch_done(db, users, clock):
    t = await task(db, users.a, clock)
    await complete(db, users.a, t.id, clock)
    assert (await drop(db, users.a, t.id, clock)).status == "done"


async def test_reopen_clears_done_at_idempotent_and_shared(db, users, clock):
    t = await task(db, users.a, clock)
    await share(db, users.a, "entry", t.id, "user", users.b.id, clock)
    await complete(db, users.a, t.id, clock)
    back = await reopen(db, users.b, t.id)
    assert back.status == "open"
    assert back.done_at is None
    assert (await reopen(db, users.b, t.id)).status == "open"
    await drop(db, users.a, t.id, clock)
    assert (await reopen(db, users.a, t.id)).status == "open"


async def test_reopen_hidden_denied(db, users, clock):
    t = await task(db, users.a, clock)
    with pytest.raises(PermissionError):
        await reopen(db, users.c, t.id)
