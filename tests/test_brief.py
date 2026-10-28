from datetime import date, timedelta

from cryptography.fernet import Fernet

from hub.access import share
from hub.brief import build, collect, render_lists
from hub.entries import create_entry
from tests.fakes import FakeLlm

MON = date(2026, 11, 2)


async def task(db, user, clock, title, due=None, kind="task"):
    return await create_entry(db, user, kind, title, due_date=due, clock=clock)


async def test_collect_groups_overdue_today_upcoming(db, users, clock):
    await task(db, users.a, clock, "старая", MON - timedelta(days=1))
    await task(db, users.a, clock, "сегодня", MON)
    await task(db, users.a, clock, "завтра", MON + timedelta(days=1))
    await task(db, users.a, clock, "через неделю", MON + timedelta(days=7))
    await task(db, users.a, clock, "позже", MON + timedelta(days=8))
    await task(db, users.a, clock, "без срока")
    facts = await collect(db, users.a, MON)
    assert [e.title for e in facts.overdue] == ["старая"]
    assert [e.title for e in facts.today] == ["сегодня"]
    assert [e.title for e in facts.upcoming] == ["завтра", "через неделю"]


async def test_think_only_on_monday(db, users, clock):
    for i in range(3):
        await task(db, users.a, clock, f"мысль {i}", kind="think")
    monday = await collect(db, users.a, MON)
    assert [e.title for e in monday.think] == ["мысль 0", "мысль 1"]
    assert (await collect(db, users.a, MON + timedelta(days=1))).think == []


async def test_render_uses_db_dates_only(db, users, clock):
    await task(db, users.a, clock, "сдать отчёт", date(2026, 11, 7))
    await task(db, users.a, clock, "просрочено", date(2026, 11, 1))
    llm = FakeLlm(intros=["Доброе утро! Сегодня 15.12 будет дело."])
    text, raw = await build(db, llm, users.a, MON)
    assert not raw
    assert "На неделе\n• сдать отчёт · 07.11" in text
    assert "Горит\n⚠️ просрочено · 01.11" in text
    lists = render_lists(await collect(db, users.a, MON))
    assert "Доброе утро" not in lists
    assert "15.12" not in lists
    assert llm.calls[0]["facts_text"] == lists  # модель видит ровно то же, что и пользователь


async def test_empty_day(db, users, clock):
    text, _ = await build(db, FakeLlm(intros=["Привет!"]), users.a, MON)
    assert text == "Привет!\n\nЗадач на сегодня нет"


async def test_build_falls_back_to_raw_on_llm_error(db, users, clock):
    await task(db, users.a, clock, "дело", MON)
    text, raw = await build(db, FakeLlm(fail=True), users.a, MON)
    assert raw
    assert text == "Горит\n• дело · 02.11"


async def test_build_truncates_to_4096(db, users, clock):
    for i in range(120):
        await task(db, users.a, clock, f"задача номер {i} " + "я" * 60, MON + timedelta(days=1))
    text, raw = await build(db, FakeLlm(intros=["Привет!"]), users.a, MON)
    assert not raw
    assert len(text) <= 4096
    last = text.splitlines()[-1]
    assert last.startswith("…ещё ")
    assert last.endswith("пунктов — /tasks")
    shown = sum(1 for ln in text.splitlines() if ln.startswith("• "))
    assert shown + int(last.split()[1]) == 120


async def test_brief_includes_shared_tasks_with_mark(db, users, clock):
    mine = await task(db, users.a, clock, "моя общая", MON)
    await task(db, users.a, clock, "моя личная", MON)
    theirs = await task(db, users.b, clock, "чужая", MON)
    await share(db, users.a, "entry", mine.id, "user", users.b.id, clock)
    await share(db, users.b, "entry", theirs.id, "user", users.a.id, clock)
    lists = render_lists(await collect(db, users.a, MON))
    assert "• моя общая · 02.11 👥" in lists
    assert "• чужая · 02.11 👥" in lists
    assert "• моя личная · 02.11\n" in lists + "\n"


async def test_brief_of_b_has_no_private_task_of_a(db, users, clock):
    await task(db, users.a, clock, "личная задача А", MON)
    await task(db, users.b, clock, "задача Б", MON)
    text, _ = await build(db, FakeLlm(intros=["Привет"]), users.b, MON)
    assert "личная задача А" not in text
    assert "задача Б" in text


async def add_event(db, user, clock, uid, starts, title, all_day=False):
    from hub.connections import add_connection
    from hub.crypto import SecretBox

    conn = await add_connection(
        db,
        SecretBox(Fernet.generate_key().decode()),
        user,
        "ics_calendar",
        "К",
        "https://x",
        "x",
        clock,
    )
    await db.execute(
        "INSERT INTO calendar_events(connection_id, uid, starts_at, ends_at, all_day, title)"
        " VALUES (?, ?, ?, NULL, ?, ?)",
        (conn.id, uid, starts, int(all_day), title),
    )
    await db.commit()


async def test_brief_has_calendar_first_with_times(db, users, clock):
    await add_event(db, users.a, clock, "1", "2026-11-02T14:30:00+03:00", "Звонок в УК")
    await add_event(db, users.a, clock, "2", "2026-11-02T00:00:00+03:00", "День рождения", True)
    await task(db, users.a, clock, "горящая", MON)
    lists = render_lists(await collect(db, users.a, MON))
    assert lists.startswith("Календарь\nвесь день · День рождения\n14:30 Звонок в УК\n\nГорит")
    assert lists.index("Календарь") < lists.index("Горит")


async def test_brief_without_calendar_unchanged(db, users, clock):
    await task(db, users.a, clock, "горящая", MON)
    facts = await collect(db, users.a, MON)
    assert facts.events == []
    assert render_lists(facts) == "Горит\n• горящая · 02.11"


async def test_truncate_drops_calendar_lines_tail_first(db, users, clock):
    from hub.brief import TG_LIMIT

    await add_event(db, users.a, clock, "seed", "2026-11-02T09:00:00+03:00", "Первое")
    conn_id = (await (await db.execute("SELECT id FROM connections")).fetchone())["id"]
    rows = [
        (conn_id, f"e{i}", f"2026-11-02T{10 + i % 12:02d}:00:00+03:00", f"Событие номер {i} " * 3)
        for i in range(300)
    ]
    await db.executemany(
        "INSERT INTO calendar_events(connection_id, uid, starts_at, ends_at, all_day, title)"
        " VALUES (?, ?, ?, NULL, 0, ?)",
        rows,
    )
    await db.commit()
    full = render_lists(await collect(db, users.a, MON)).split("\n")
    text, _ = await build(db, FakeLlm(intros=["Привет"]), users.a, MON)
    assert len(text) <= TG_LIMIT
    *kept, footer = text.split("\n")
    assert "ещё" in footer
    assert kept[:2] == ["Привет", ""]
    kept = kept[2:]
    assert 3 < len(kept) < len(full)
    assert kept == full[: len(kept)]  # убраны только последние строки


async def test_truncate_keeps_intro_lines_that_look_like_items(db, users, clock):
    intro = "10:00 это строка вступления\nвесь день · тоже вступление"
    for i in range(400):
        await task(db, users.a, clock, f"Задача {i} " * 3, MON)
    text, _ = await build(db, FakeLlm(intros=[intro]), users.a, MON)
    assert text.startswith(intro)


async def test_build_full_survives_oversized_intro(db, users, clock):
    from hub.brief import build_full

    intro = "в" * 4200
    text, raw, got = await build_full(db, FakeLlm(intros=[intro]), users.a, MON)
    assert raw is False
    assert got == intro
    assert text.startswith(intro)
