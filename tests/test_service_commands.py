from datetime import timedelta

from hub.access import share
from hub.auth import redeem_login_link
from hub.entries import create_entry
from hub.sections import get_or_create_section
from hub.service import NO_ACCESS, Service
from hub.users import family_space_id
from tests.fakes import FakeLlm


def make(db, clock, settings):
    return Service(db, FakeLlm(), clock, settings)


async def task(db, owner, clock, title, due=None, **kw):
    return await create_entry(db, owner, "task", title, due_date=due, clock=clock, **kw)


def buttons_flat(reply):
    return [b for row in reply.buttons for b in row]


async def test_tasks_shows_overdue_first_and_shared_mark(db, users, clock, settings):
    svc = make(db, clock, settings)
    today = clock.today()
    await task(db, users.a, clock, "Без срока")
    await task(db, users.a, clock, "Сегодня", today)
    late = await task(db, users.a, clock, "Просрочено", today - timedelta(days=2))
    mine_shared = await task(db, users.a, clock, "Моя общая")
    theirs = await task(db, users.b, clock, "Чужая")
    await share(db, users.b, "entry", theirs.id, "user", users.a.id, clock)
    await share(db, users.a, "entry", mine_shared.id, "user", users.b.id, clock)
    reply = await svc.tasks(users.a.tg_id)
    lines = reply.text.splitlines()
    assert lines[0] == f"⚠️ 1. Просрочено · {today - timedelta(days=2):%d.%m}"
    assert lines[1].startswith("• 2. Сегодня · ")
    assert "👥" not in lines[1]
    by_title = {ln.split(". ", 1)[1].split(" ·")[0].replace(" 👥", ""): ln for ln in lines}
    assert by_title["Чужая"].endswith("👥")
    assert by_title["Моя общая"].endswith("👥")
    assert "👥" not in by_title["Без срока"]
    assert buttons_flat(reply)[0].text == "✔ 1"
    assert buttons_flat(reply)[0].data == f"d:{late.id}"


async def test_tasks_caps_at_20(db, users, clock, settings):
    svc = make(db, clock, settings)
    for i in range(23):
        await task(db, users.a, clock, f"Задача {i}")
    reply = await svc.tasks(users.a.tg_id)
    assert len(reply.text.splitlines()) == 21
    assert reply.text.splitlines()[-1] == "и ещё 3"
    assert [len(r) for r in reply.buttons] == [4, 4, 4, 4, 4]
    assert buttons_flat(reply)[-1].text == "✔ 20"
    assert all(b.style == "success" for b in buttons_flat(reply))


async def test_double_done_is_idempotent(db, users, clock, settings):
    svc = make(db, clock, settings)
    t = await task(db, users.a, clock, "Сделать")
    first = await svc.callback(users.a.tg_id, f"d:{t.id}")
    assert first.text == "✔ Готово: Сделать"
    again = await svc.callback(users.a.tg_id, f"d:{t.id}")
    assert again.text == "Уже сделано"


async def test_share_buttons_list_other_users_and_family(db, users, clock, settings):
    svc = make(db, clock, settings)
    t = await task(db, users.a, clock, "Общее")
    reply = await svc.callback(users.a.tg_id, f"sh:{t.id}")
    space = await family_space_id(db)
    got = {b.text: b.data for b in buttons_flat(reply)}
    assert got == {
        "Б": f"shu:{t.id}:{users.b.id}",
        "В": f"shu:{t.id}:{users.c.id}",
        "Семья": f"shs:{t.id}:{space}",
    }
    await share(db, users.a, "entry", t.id, "user", users.b.id, clock)
    assert (await svc.callback(users.b.tg_id, f"sh:{t.id}")).text == NO_ACCESS
    assert (await svc.callback(users.b.tg_id, f"shu:{t.id}:{users.c.id}")).text == NO_ACCESS


async def test_share_then_other_user_sees_in_tasks(db, users, clock, settings):
    svc = make(db, clock, settings)
    t = await task(db, users.a, clock, "Купить хлеб")
    assert (await svc.tasks(users.b.tg_id)).text == "Открытых задач нет"
    reply = await svc.callback(users.a.tg_id, f"shu:{t.id}:{users.b.id}")
    assert reply.text == "Поделился: Б"
    seen = await svc.tasks(users.b.tg_id)
    assert "Купить хлеб" in seen.text
    assert seen.text.endswith("👥")
    space = await family_space_id(db)
    t2 = await task(db, users.a, clock, "Для семьи")
    assert (await svc.callback(users.a.tg_id, f"shs:{t2.id}:{space}")).text == "Поделился: Семья"
    assert "Для семьи" in (await svc.tasks(users.b.tg_id)).text


async def test_section_buttons_and_assignment(db, users, clock, settings):
    svc = make(db, clock, settings)
    t = await task(db, users.a, clock, "Билеты")
    trip = await get_or_create_section(db, users.a, "Отпуск")
    await get_or_create_section(db, users.b, "Чужой")
    reply = await svc.callback(users.a.tg_id, f"s:{t.id}")
    assert [(b.text, b.data) for b in buttons_flat(reply)] == [
        ("Отпуск", f"ss:{t.id}:{trip.id}"),
        ("Без раздела", f"ss:{t.id}:0"),
    ]
    moved = await svc.callback(users.a.tg_id, f"ss:{t.id}:{trip.id}")
    assert moved.text == "📌 Задача: Билеты · раздел Отпуск"
    cleared = await svc.callback(users.a.tg_id, f"ss:{t.id}:0")
    assert cleared.text == "📌 Задача: Билеты"
    assert (await svc.callback(users.b.tg_id, f"s:{t.id}")).text == NO_ACCESS


async def test_parse_is_strict(db, users, clock, settings):
    svc = make(db, clock, settings)
    for bad in ("ss:1", "ss:1:x", "shu:1:-1", "ss:1:2:3", "shs::1"):
        assert await svc.callback(users.a.tg_id, bad) is None


async def test_section_command_moves_last_entry_within_10_min(db, users, clock, settings):
    svc = make(db, clock, settings)
    await task(db, users.a, clock, "Старая")
    other = await task(db, users.b, clock, "Чужая")
    clock._dt += timedelta(minutes=9)
    reply = await svc.section_command(users.a.tg_id, "Отпуск")
    assert reply.text == "📌 Задача: Старая · раздел Отпуск"
    sql = "SELECT section_id FROM entries WHERE id = ?"
    row = await (await db.execute(sql, (other.id,))).fetchone()
    assert row["section_id"] is None


async def test_section_command_ignores_older_entry(db, users, clock, settings):
    svc = make(db, clock, settings)
    t = await task(db, users.a, clock, "Давно")
    clock._dt += timedelta(minutes=11)
    reply = await svc.section_command(users.a.tg_id, "Отпуск")
    assert "Отпуск" in reply.text
    sql = "SELECT section_id FROM entries WHERE id = ?"
    row = await (await db.execute(sql, (t.id,))).fetchone()
    assert row["section_id"] is None
    assert (await svc.sections(users.a.tg_id)).text == "• Отпуск — 0"


async def test_section_command_empty_name_and_sections_counts(db, users, clock, settings):
    svc = make(db, clock, settings)
    assert "/section <имя раздела>" in (await svc.section_command(users.a.tg_id, "  ")).text
    assert "/section <имя раздела>" in (await svc.sections(users.a.tg_id)).text
    sec = await get_or_create_section(db, users.a, "Дом")
    await task(db, users.a, clock, "Один", section_id=sec.id)
    await task(db, users.a, clock, "Два", section_id=sec.id)
    assert (await svc.sections(users.a.tg_id)).text == "• Дом — 2"


async def test_new_commands_ignore_strangers(db, users, clock, settings):
    svc = make(db, clock, settings)
    assert await svc.tasks(999) is None
    assert await svc.sections(999) is None
    assert await svc.section_command(999, "Х") is None
    row = await (await db.execute("SELECT COUNT(*) AS n FROM sections")).fetchone()
    assert row["n"] == 0


async def test_brief_command_replies_without_touching_briefs(db, users, clock, settings):
    svc = Service(db, FakeLlm(intros=["Доброе утро!"]), clock, settings)
    reply = await svc.brief(users.a.tg_id)
    assert reply.text.startswith("Доброе утро!")
    assert reply.buttons == []
    assert await svc.brief(999) is None
    assert (await (await db.execute("SELECT COUNT(*) AS n FROM briefs")).fetchone())["n"] == 0


async def test_help_only_for_registered(db, users, clock, settings):
    svc = make(db, clock, settings)
    assert await svc.help(999) is None
    reply = await svc.help(users.a.tg_id)
    assert "/tasks" in reply.text


async def test_web_link_for_registered_only(db, users, clock, settings):
    svc = make(db, clock, settings)
    assert await svc.web_link(999) is None
    reply = await svc.web_link(users.a.tg_id)
    assert reply.text.startswith("Ссылка для входа на 10 минут:\n")
    assert f"{settings.web_base_url}/auth/link?t=" in reply.text
    token = reply.text.split("?t=")[1]
    assert (await redeem_login_link(db, token, clock, 30)) is not None


async def test_settings_command_has_web_app_button(db, users, clock, settings):
    svc = make(db, clock, settings)
    assert await svc.settings_button(999) is None
    reply = await svc.settings_button(users.a.tg_id)
    button = reply.buttons[0][0]
    assert button.text == "⚙️ Настройки"
    assert button.web_app == f"{settings.web_base_url}/settings"


async def test_help_lists_web_and_settings(db, users, clock, settings):
    reply = await make(db, clock, settings).help(users.a.tg_id)
    assert "/web" in reply.text
    assert "/settings" in reply.text
