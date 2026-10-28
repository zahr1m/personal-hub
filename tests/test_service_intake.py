import asyncio
from datetime import date

from hub.access import share
from hub.budget import BudgetExceeded
from hub.entries import children, create_entry
from hub.llm import Classification, LlmError
from hub.sections import get_or_create_section
from hub.service import Service
from hub.users import create_invite, get_user_by_tg
from tests.fakes import FakeLlm


def cls(kind="task", title="Купить билеты", due=None, section=None):
    return Classification(kind, title, due, None, section)


def make(db, clock, settings, **kw):
    llm = FakeLlm(**kw)
    return Service(db, llm, clock, settings), llm


async def count_entries(db):
    row = await (await db.execute("SELECT COUNT(*) AS n FROM entries")).fetchone()
    return row["n"]


async def get_entry(db, entry_id):
    return await (await db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,))).fetchone()


def entry_id_of(reply, prefix="k:"):
    data = next(b.data for row in reply.buttons for b in row if b.data.startswith(prefix))
    return int(data.split(":")[1])


class FailingLlm(FakeLlm):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    async def classify(self, user_id, text, today, sections):
        raise self.exc


async def test_stranger_is_ignored_and_nothing_saved(db, users, clock, settings):
    svc, llm = make(db, clock, settings, classifications=[cls()])
    assert await svc.text(999, "купить молоко") is None
    assert await svc.start(999, "Чужой", None) is None
    assert await svc.start(999, "Чужой", "nonexistent") is None
    assert await svc.invite(999, "bot") is None
    assert await svc.callback(999, "d:1") is None
    assert await count_entries(db) == 0
    assert llm.calls == []


async def test_stranger_with_used_code_is_ignored(db, users, clock, settings):
    svc, _ = make(db, clock, settings)
    code = await create_invite(db, users.a, clock)
    assert await svc.start(50, "Новый", code) is not None
    assert await svc.start(51, "Другой", code) is None
    assert await svc.text(51, "привет") is None


async def test_start_greeting_mentions_admin_access(db, users, clock, settings):
    svc, _ = make(db, clock, settings)
    code = await create_invite(db, users.a, clock)
    reply = await svc.start(50, "Новый", code)
    assert "Администратор сервера технически может видеть данные на нём." in reply.text
    again = await svc.start(50, "Новый", None)
    assert again is not None


async def test_invite_for_admin_only(db, users, clock, settings):
    svc, _ = make(db, clock, settings)
    reply = await svc.invite(users.a.tg_id, "my_bot")
    assert reply.text.count("https://t.me/my_bot?start=") == 1
    assert "действует 24 часа" in reply.text
    assert await svc.invite(users.b.tg_id, "my_bot") is None


async def test_text_creates_task_with_due_and_section(db, users, clock, settings):
    section = await get_or_create_section(db, users.a, "Отпуск")
    svc, llm = make(
        db,
        clock,
        settings,
        classifications=[cls(due=date(2026, 11, 7), section="Отпуск")],
    )
    reply = await svc.text(users.a.tg_id, "купить билеты до пятницы")
    assert reply.text == "📌 Задача: Купить билеты · до 07.11 · раздел Отпуск"
    assert llm.calls[0]["today"] == date(2026, 11, 2)
    assert llm.calls[0]["sections"] == ["Отпуск"]
    eid = entry_id_of(reply)
    row = await get_entry(db, eid)
    assert row["section_id"] == section.id
    assert row["due_date"] == "2026-11-07"
    datas = [b.data for r in reply.buttons for b in r]
    assert {f"k:{eid}:task", f"k:{eid}:project", f"k:{eid}:think", f"k:{eid}:note"} <= set(datas)
    assert {f"s:{eid}", f"sh:{eid}", f"d:{eid}"} <= set(datas)


async def test_invisible_section_is_dropped(db, users, clock, settings):
    await get_or_create_section(db, users.b, "Чужой")
    svc, _ = make(db, clock, settings, classifications=[cls(section="Чужой")])
    reply = await svc.text(users.a.tg_id, "x")
    assert "раздел" not in reply.text
    assert (await get_entry(db, entry_id_of(reply)))["section_id"] is None


async def test_shared_section_is_used(db, users, clock, settings):
    section = await get_or_create_section(db, users.b, "Общий")
    await share(db, users.b, "section", section.id, "user", users.a.id, clock)
    svc, _ = make(db, clock, settings, classifications=[cls(section="Общий")])
    reply = await svc.text(users.a.tg_id, "x")
    assert (await get_entry(db, entry_id_of(reply)))["section_id"] == section.id


async def test_llm_failure_saves_note(db, users, clock, settings):
    svc, _ = make(db, clock, settings)
    svc._llm = FailingLlm(LlmError("boom"))
    text = "длинный текст " * 20
    reply = await svc.text(users.a.tg_id, text)
    assert "Не разобрал, сохранил как заметку" in reply.text
    assert "лимит" not in reply.text
    row = await get_entry(db, entry_id_of(reply))
    assert row["kind"] == "note"
    assert row["title"] == text[:120].strip()
    assert row["body"] == text
    assert any(b.data.startswith("k:") for r in reply.buttons for b in r)


async def test_budget_exceeded_saves_note_and_says_so(db, users, clock, settings):
    svc, _ = make(db, clock, settings)
    svc._llm = FailingLlm(BudgetExceeded("лимит"))
    reply = await svc.text(users.a.tg_id, "мысль")
    assert "Не разобрал, сохранил как заметку" in reply.text
    assert "дневной лимит модели исчерпан" in reply.text
    assert (await get_entry(db, entry_id_of(reply)))["kind"] == "note"


async def test_project_proposes_subtasks_and_creates_on_ok(db, users, clock, settings):
    svc, llm = make(
        db,
        clock,
        settings,
        classifications=[cls("project", "Ремонт кухни")],
        subtasks=[["Замерить", "Выбрать плитку", "Нанять мастера"]],
    )
    reply = await svc.text(users.a.tg_id, "хочу ремонт кухни")
    pid = entry_id_of(reply, "pj:")
    assert "1. Замерить" in reply.text
    assert "3. Нанять мастера" in reply.text
    datas = [b.data for r in reply.buttons for b in r]
    assert f"pj:{pid}:ok" in datas
    assert f"pj:{pid}:no" in datas
    assert "Подзадачи (предложение):" in (await get_entry(db, pid))["body"]

    ok = await svc.callback(users.a.tg_id, f"pj:{pid}:ok")
    assert ok is not None
    kids = await children(db, users.a, pid)
    assert [k.title for k in kids] == ["Замерить", "Выбрать плитку", "Нанять мастера"]
    assert all(k.kind == "task" and k.parent_id == pid for k in kids)
    assert "Подзадачи (предложение)" not in (await get_entry(db, pid))["body"]
    assert "хочу ремонт кухни" in (await get_entry(db, pid))["body"]

    await svc.callback(users.a.tg_id, f"pj:{pid}:ok")  # повтор не дублирует
    assert len(await children(db, users.a, pid)) == 3
    assert [c["method"] for c in llm.calls] == ["classify", "decompose"]


async def test_project_no_removes_proposal(db, users, clock, settings):
    svc, _ = make(
        db,
        clock,
        settings,
        classifications=[cls("project", "Ремонт")],
        subtasks=[["а", "б", "в"]],
    )
    pid = entry_id_of(await svc.text(users.a.tg_id, "ремонт"), "pj:")
    await svc.callback(users.a.tg_id, f"pj:{pid}:no")
    assert "Подзадачи (предложение)" not in (await get_entry(db, pid))["body"]
    await svc.callback(users.a.tg_id, f"pj:{pid}:ok")
    assert await children(db, users.a, pid) == []


class DecomposeFails(FakeLlm):
    async def decompose(self, user_id, title, body):
        raise LlmError("boom")


async def test_project_decompose_failure_keeps_project(db, users, clock, settings):
    llm = DecomposeFails(classifications=[cls("project", "Ремонт")])
    svc = Service(db, llm, clock, settings)
    reply = await svc.text(users.a.tg_id, "ремонт")
    assert "не удалось" in reply.text
    assert not any(b.data.startswith("pj:") for r in reply.buttons for b in r)
    row = await get_entry(db, entry_id_of(reply))
    assert row["kind"] == "project"
    assert "Подзадачи (предложение)" not in row["body"]


async def test_change_kind_by_button(db, users, clock, settings):
    svc, _ = make(db, clock, settings, classifications=[cls("task", "Идея")])
    eid = entry_id_of(await svc.text(users.a.tg_id, "идея"))
    reply = await svc.callback(users.a.tg_id, f"k:{eid}:think")
    assert (await get_entry(db, eid))["kind"] == "think"
    assert "Подумать" in reply.text
    assert not any(b.data.startswith("d:") for r in reply.buttons for b in r)


async def test_switch_to_project_proposes_subtasks_and_creates_on_ok(db, users, clock, settings):
    svc, _ = make(
        db,
        clock,
        settings,
        classifications=[cls("task", "Переоформить машину")],
        subtasks=[["Собрать документы", "Снять с учёта", "Поставить на учёт"]],
    )
    eid = entry_id_of(await svc.text(users.a.tg_id, "переоформить машину"))
    reply = await svc.callback(users.a.tg_id, f"k:{eid}:project")
    assert "Проект: Переоформить машину" in reply.text
    assert "1. Собрать документы" in reply.text
    datas = [b.data for r in reply.buttons for b in r]
    assert f"pj:{eid}:ok" in datas
    assert f"pj:{eid}:no" in datas
    await svc.callback(users.a.tg_id, f"pj:{eid}:ok")
    kids = await children(db, users.a, eid)
    assert [k.title for k in kids] == ["Собрать документы", "Снять с учёта", "Поставить на учёт"]


async def test_switch_to_project_with_existing_proposal_does_not_redecompose(
    db, users, clock, settings
):
    svc, llm = make(
        db,
        clock,
        settings,
        classifications=[cls("project", "Ремонт")],
        subtasks=[["а", "б", "в"]],
    )
    eid = entry_id_of(await svc.text(users.a.tg_id, "ремонт"), "pj:")
    await svc.callback(users.a.tg_id, f"k:{eid}:think")
    reply = await svc.callback(users.a.tg_id, f"k:{eid}:project")
    assert "Проект: Ремонт" in reply.text
    assert "Предлагаю" not in reply.text
    assert [c["method"] for c in llm.calls] == ["classify", "decompose"]
    await svc.callback(users.a.tg_id, f"k:{eid}:project")  # уже проект — тоже без вызова
    assert [c["method"] for c in llm.calls] == ["classify", "decompose"]


async def test_switch_to_project_decompose_failure_keeps_project(db, users, clock, settings):
    llm = DecomposeFails(classifications=[cls("task", "Переезд")])
    svc = Service(db, llm, clock, settings)
    eid = entry_id_of(await svc.text(users.a.tg_id, "переезд"))
    reply = await svc.callback(users.a.tg_id, f"k:{eid}:project")
    assert "Подзадачи предложить не удалось" in reply.text
    assert not any(b.data.startswith("pj:") for r in reply.buttons for b in r)
    assert (await get_entry(db, eid))["kind"] == "project"


async def test_done_button(db, users, clock, settings):
    svc, _ = make(db, clock, settings, classifications=[cls()])
    eid = entry_id_of(await svc.text(users.a.tg_id, "x"))
    reply = await svc.callback(users.a.tg_id, f"d:{eid}")
    assert "✔" in reply.text
    assert (await get_entry(db, eid))["status"] == "done"


async def test_task_buttons_done_and_cancel_with_styles(db, users, clock, settings):
    svc, _ = make(db, clock, settings, classifications=[cls()])
    reply = await svc.text(users.a.tg_id, "x")
    eid = entry_id_of(reply)
    by_data = {b.data: b for r in reply.buttons for b in r}
    assert (by_data[f"d:{eid}"].text, by_data[f"d:{eid}"].style) == ("✅ Выполнено", "success")
    assert (by_data[f"x:{eid}"].text, by_data[f"x:{eid}"].style) == ("❌ Отменить", "danger")


async def test_done_has_return_button_and_reopen_restores(db, users, clock, settings):
    svc, _ = make(db, clock, settings, classifications=[cls()])
    eid = entry_id_of(await svc.text(users.a.tg_id, "x"))
    done = await svc.callback(users.a.tg_id, f"d:{eid}")
    ret = done.buttons[0][0]
    assert (ret.text, ret.data, ret.style) == ("↩️ Вернуть в работу", f"r:{eid}", "primary")
    back = await svc.callback(users.a.tg_id, f"r:{eid}")
    assert (await get_entry(db, eid))["status"] == "open"
    assert (await get_entry(db, eid))["done_at"] is None
    assert any(b.data == f"d:{eid}" for r in back.buttons for b in r)
    again = await svc.callback(users.a.tg_id, f"r:{eid}")  # идемпотентно
    assert again is not None
    assert (await get_entry(db, eid))["status"] == "open"


async def test_cancel_drops_and_offers_return(db, users, clock, settings):
    svc, _ = make(db, clock, settings, classifications=[cls("task", "Купить билеты")])
    eid = entry_id_of(await svc.text(users.a.tg_id, "x"))
    reply = await svc.callback(users.a.tg_id, f"x:{eid}")
    assert "Купить билеты" in reply.text
    assert "Отменено" in reply.text
    assert reply.buttons[0][0].data == f"r:{eid}"
    assert (await get_entry(db, eid))["status"] == "dropped"
    assert (await svc.callback(users.a.tg_id, f"d:{eid}")).text == "Задача отменена"
    assert (await get_entry(db, eid))["status"] == "dropped"
    await svc.callback(users.a.tg_id, f"r:{eid}")
    assert (await get_entry(db, eid))["status"] == "open"


async def test_cancel_by_non_owner_denied(db, users, clock, settings):
    svc, _ = make(db, clock, settings)
    entry = await create_entry(db, users.a, "task", "t", "t", clock=clock)
    await share(db, users.a, "entry", entry.id, "user", users.b.id, clock)
    reply = await svc.callback(users.b.tg_id, f"x:{entry.id}")
    assert reply.text == "Нет доступа"
    assert (await get_entry(db, entry.id))["status"] == "open"
    assert (await svc.callback(users.b.tg_id, f"r:{entry.id}")) is not None


async def test_callback_on_hidden_entry_says_no_access(db, users, clock, settings):
    entry = await create_entry(db, users.b, "task", "чужая", clock=clock)
    svc, _ = make(db, clock, settings)
    for data in (f"d:{entry.id}", f"k:{entry.id}:note", f"pj:{entry.id}:ok", f"s:{entry.id}"):
        reply = await svc.callback(users.a.tg_id, data)
        assert reply.text == "Нет доступа"
    assert (await get_entry(db, entry.id))["status"] == "open"


async def test_visible_but_not_owned_kind_change_denied(db, users, clock, settings):
    entry = await create_entry(db, users.b, "task", "общая", clock=clock)
    await share(db, users.b, "entry", entry.id, "user", users.a.id, clock)
    svc, _ = make(db, clock, settings)
    reply = await svc.callback(users.a.tg_id, f"k:{entry.id}:note")
    assert reply.text == "Нет доступа"
    assert (await get_entry(db, entry.id))["kind"] == "task"


async def test_unparseable_callback_is_silent(db, users, clock, settings):
    svc, _ = make(db, clock, settings)
    for data in (
        "",
        "zzz",
        "k:abc:task",
        "k:1:bogus",
        "d:",
        "pj:1:maybe",
        "x",
        "x:1:2",
        "r:abc",
        "r:1:2",
    ):
        assert await svc.callback(users.a.tg_id, data) is None


async def test_button_data_under_64_bytes(db, users, clock, settings):
    svc, _ = make(
        db,
        clock,
        settings,
        classifications=[cls("project", "П")],
        subtasks=[["а", "б", "в"]],
    )
    reply = await svc.text(users.a.tg_id, "п")
    datas = [b.data for r in reply.buttons for b in r]
    assert datas
    assert all(len(d.encode()) <= 64 for d in datas)


async def _proposed_project(db, users, clock, settings, text="ремонт"):
    svc, _ = make(
        db,
        clock,
        settings,
        classifications=[cls("project", "Ремонт")],
        subtasks=[["а", "б", "в"]],
    )
    pid = entry_id_of(await svc.text(users.b.tg_id, text), "pj:")
    await share(db, users.b, "entry", pid, "user", users.a.id, clock)
    return svc, pid


async def test_project_choice_is_owner_only(db, users, clock, settings):
    svc, pid = await _proposed_project(db, users, clock, settings)
    before = (await get_entry(db, pid))["body"]
    for action in ("ok", "no"):
        reply = await svc.callback(users.a.tg_id, f"pj:{pid}:{action}")
        assert reply.text == "Нет доступа"
    assert await children(db, users.b, pid) == []
    assert (await get_entry(db, pid))["body"] == before


async def test_proposal_ignored_for_non_project(db, users, clock, settings):
    svc, _ = make(db, clock, settings)
    note = await create_entry(
        db,
        users.a,
        "note",
        "н",
        "x\n\nПодзадачи (предложение):\n- левая",
        clock=clock,
    )
    reply = await svc.callback(users.a.tg_id, f"pj:{note.id}:ok")
    assert reply.text == "Нечего создавать"
    assert await children(db, users.a, note.id) == []


async def test_user_text_with_header_survives(db, users, clock, settings):
    own = "план\nПодзадачи (предложение):\n- своё"
    svc, _ = make(
        db,
        clock,
        settings,
        classifications=[cls("project", "П"), cls("project", "П")],
        subtasks=[["а", "б", "в"], ["а", "б", "в"]],
    )
    pid = entry_id_of(await svc.text(users.a.tg_id, own), "pj:")
    await svc.callback(users.a.tg_id, f"pj:{pid}:ok")
    assert [k.title for k in await children(db, users.a, pid)] == ["а", "б", "в"]
    assert (await get_entry(db, pid))["body"] == own
    pid2 = entry_id_of(await svc.text(users.a.tg_id, own), "pj:")
    await svc.callback(users.a.tg_id, f"pj:{pid2}:no")
    assert (await get_entry(db, pid2))["body"] == own


async def test_on_register_called_once_for_new_user(db, users, clock, settings):
    seen = []

    async def on_register(user):
        seen.append(user)

    svc = Service(db, FakeLlm(), clock, settings, on_register=on_register)
    code = await create_invite(db, users.a, clock)
    assert await svc.start(50, "Новый", code) is not None
    assert [(u.tg_id, u.name) for u in seen] == [(50, "Новый")]
    # уже зарегистрированный и использованный код — без вызова
    assert await svc.start(50, "Новый", None) is not None
    assert await svc.start(51, "Другой", code) is None
    assert len(seen) == 1


async def test_on_register_failure_does_not_break_welcome(db, users, clock, settings):
    async def boom(user):
        raise RuntimeError("scheduler down")

    svc = Service(db, FakeLlm(), clock, settings, on_register=boom)
    code = await create_invite(db, users.a, clock)
    reply = await svc.start(60, "Новый", code)
    assert reply is not None
    assert "Ты в списке" in reply.text
    assert await get_user_by_tg(db, 60) is not None


async def test_double_click_create_subtasks_makes_no_duplicates(db, users, clock, settings):
    svc, _ = make(
        db,
        clock,
        settings,
        classifications=[cls("project", "Ремонт")],
        subtasks=[["Замерить", "Выбрать плитку", "Нанять мастера"]],
    )
    pid = entry_id_of(await svc.text(users.a.tg_id, "ремонт"), "pj:")
    first, second = await asyncio.gather(
        svc.callback(users.a.tg_id, f"pj:{pid}:ok"), svc.callback(users.a.tg_id, f"pj:{pid}:ok")
    )
    assert len(await children(db, users.a, pid)) == 3
    texts = sorted([first.text, second.text])
    assert "Уже создано" in texts[1] or "Уже создано" in texts[0]
    assert "Подзадачи (предложение)" not in (await get_entry(db, pid))["body"]


async def test_subtasks_inherit_project_section(db, users, clock, settings):
    section = await get_or_create_section(db, users.a, "Дом")
    svc, _ = make(
        db,
        clock,
        settings,
        classifications=[cls("project", "Ремонт", section="Дом")],
        subtasks=[["а", "б", "в"]],
    )
    pid = entry_id_of(await svc.text(users.a.tg_id, "ремонт"), "pj:")
    await svc.callback(users.a.tg_id, f"pj:{pid}:ok")
    kids = await children(db, users.a, pid)
    assert [k.section_id for k in kids] == [section.id] * 3


async def test_whitelist_blocks_stranger_and_keeps_code(db, users, clock, settings):
    settings.allowed_tg_ids = [50]
    svc, _ = make(db, clock, settings)
    code = await create_invite(db, users.a, clock)
    assert await svc.start(60, "Чужой", code) is None
    assert await get_user_by_tg(db, 60) is None
    assert await svc.start(50, "Свой", code) is not None  # код не потрачен


async def test_whitelist_empty_means_open_and_admin_always_allowed(db, users, clock, settings):
    svc, _ = make(db, clock, settings)
    assert await svc.start(70, "Любой", await create_invite(db, users.a, clock)) is not None
    settings.allowed_tg_ids = [50]
    assert await svc.start(users.a.tg_id, "А", None) is not None


async def test_registration_takes_name_from_map(db, users, clock, settings):
    settings.user_names = {50: "Боря"}
    svc, _ = make(db, clock, settings)
    reply = await svc.start(50, "tg-имя", await create_invite(db, users.a, clock))
    assert "Боря" in reply.text
    assert (await get_user_by_tg(db, 50)).name == "Боря"


async def test_pick_section_prefers_own_over_shared_with_same_name(db, users, clock, settings):
    theirs = await get_or_create_section(db, users.b, "Дом")  # id меньше
    mine = await get_or_create_section(db, users.a, "Дом")
    await share(db, users.b, "section", theirs.id, "user", users.a.id, clock)
    svc, _ = make(db, clock, settings, classifications=[cls("task", "Купить", section="Дом")])
    reply = await svc.text(users.a.tg_id, "купить")
    row = await get_entry(db, entry_id_of(reply))
    assert row["section_id"] == mine.id


async def test_add_entry_returns_entry_and_none_note(db, users, clock, settings):
    svc, _ = make(db, clock, settings, classifications=[cls()])
    entry, note = await svc.add_entry(users.a, "купить билеты")
    assert note is None
    assert entry.kind == "task"
    assert (await get_entry(db, entry.id))["title"] == "Купить билеты"


async def test_add_entry_llm_failure_note(db, users, clock, settings):
    svc, _ = make(db, clock, settings)
    svc._llm = FailingLlm(BudgetExceeded("лимит"))
    entry, note = await svc.add_entry(users.a, "мысль")
    assert entry.kind == "note"
    assert note == "Не разобрал, сохранил как заметку\nдневной лимит модели исчерпан"
    svc._llm = FailingLlm(LlmError("boom"))
    _, note = await svc.add_entry(users.a, "ещё мысль")
    assert note == "Не разобрал, сохранил как заметку"


async def test_add_entry_project_writes_proposal_or_notes_failure(db, users, clock, settings):
    svc, _ = make(
        db, clock, settings, classifications=[cls("project", "Ремонт")], subtasks=[["а", "б"]]
    )
    entry, note = await svc.add_entry(users.a, "ремонт")
    assert note is None
    assert "Подзадачи (предложение):" in entry.body
    assert entry.body == (await get_entry(db, entry.id))["body"]
    failing = Service(
        db, DecomposeFails(classifications=[cls("project", "Ремонт")]), clock, settings
    )
    entry, note = await failing.add_entry(users.a, "ремонт")
    assert note == "Подзадачи предложить не удалось."
    assert "Подзадачи (предложение)" not in entry.body


async def test_proposal_owner_only(db, users, clock, settings):
    svc, _ = make(
        db,
        clock,
        settings,
        classifications=[cls("project", "Ремонт"), cls("task", "Купить")],
        subtasks=[["Замерить", "Выбрать плитку"]],
    )
    project, _ = await svc.add_entry(users.a, "ремонт")
    task, _ = await svc.add_entry(users.a, "купить")
    assert await svc.proposal(users.a, project.id) == ["Замерить", "Выбрать плитку"]
    assert await svc.proposal(users.b, project.id) is None
    assert await svc.proposal(users.a, task.id) is None
    assert await svc.proposal(users.a, 9999) is None
    await svc.callback(users.a.tg_id, f"pj:{project.id}:no")
    assert await svc.proposal(users.a, project.id) is None
