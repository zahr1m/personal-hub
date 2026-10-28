from datetime import date, timedelta

import httpx
import pytest

from hub.access import share
from hub.auth import create_login_link
from hub.crypto import SecretBox
from hub.entries import children, create_entry
from hub.llm import Classification
from hub.sections import get_or_create_section
from hub.service import PROPOSAL_HEADER, Service
from hub.web import create_app
from tests.fakes import FakeLlm

HX = {"HX-Request": "true"}
SECRET = "СекретнаяЗадачаБ"


@pytest.fixture
def llm():
    return FakeLlm()


@pytest.fixture
def app(db, settings, clock, llm):
    return create_app(
        db,
        service=Service(db, llm, clock, settings),
        settings=settings,
        clock=clock,
        box=SecretBox(settings.connections_key),
        http_client_factory=httpx.AsyncClient,
    )


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        yield c


async def login(client, db, user, clock):
    client.cookies.clear()
    link = await create_login_link(db, user, clock)
    assert (await client.get("/auth/link", params={"t": link})).status_code == 303


async def task(db, clock, owner, title, due=None, **kw):
    return await create_entry(db, owner, "task", title, due_date=due, clock=clock, **kw)


async def test_tasks_page_groups_and_filters(client, db, users, clock):
    today = clock.today()
    await task(db, clock, users.a, "Старая", today - timedelta(days=3))
    await task(db, clock, users.a, "Вчерашняя", today - timedelta(days=1))
    await task(db, clock, users.a, "Сегодняшняя", today)
    await task(db, clock, users.a, "Скоро", today + timedelta(days=3))
    await task(db, clock, users.a, "Потом", today + timedelta(days=30))
    await task(db, clock, users.a, "Без срока")
    await create_entry(db, users.a, "think", "Обдумать", clock=clock)
    sec = await get_or_create_section(db, users.a, "Дом")
    await task(db, clock, users.a, "В доме", section_id=sec.id)
    theirs = await task(db, clock, users.b, "Задача партнёра")
    await share(db, users.b, "entry", theirs.id, "user", users.a.id, clock)
    await task(db, clock, users.b, SECRET)

    await login(client, db, users.a, clock)
    page = (await client.get("/tasks")).text
    for label in ("Просрочено", "Сегодня", "На неделе", "Позже", "Без срока", "Подумать"):
        assert label in page
    assert "просрочено 3 дн." in page
    assert "вчера" in page
    assert "Задача партнёра" in page
    assert SECRET not in page
    assert "Дом" in page  # плашка раздела

    mine = (await client.get("/tasks", params={"f": "mine"})).text
    assert "Старая" in mine
    assert "Задача партнёра" not in mine

    only_sec = (await client.get("/tasks", params={"s": sec.id})).text
    assert "В доме" in only_sec
    assert "Старая" not in only_sec


async def test_shared_filter_shows_partner_shared_task(client, db, users, clock):
    theirs = await task(db, clock, users.b, "Задача партнёра")
    await share(db, users.b, "entry", theirs.id, "user", users.a.id, clock)
    await task(db, clock, users.a, "Моя личная")
    mine_shared = await task(db, clock, users.a, "Моя общая")
    await share(db, users.a, "entry", mine_shared.id, "user", users.b.id, clock)

    await login(client, db, users.a, clock)
    page = (await client.get("/tasks", params={"f": "shared"})).text
    assert "Задача партнёра" in page
    assert "Моя общая" in page
    assert "Моя личная" not in page
    assert 'aria-label="Общая"' in page


async def test_add_entry_via_web_creates_task(client, db, users, clock, llm):
    llm.classifications.append(Classification("task", "Купить хлеб", None, None, None))
    await login(client, db, users.a, clock)
    r = await client.post("/entries", data={"text": "купить хлеб"}, headers=HX)
    assert r.status_code == 200
    assert "Купить хлеб" in r.text
    assert (await client.get("/tasks")).text.count("Купить хлеб") == 1


async def test_add_entry_not_parsed_shows_toast(client, db, users, clock, llm):
    llm.fail = True
    await login(client, db, users.a, clock)
    r = await client.post("/entries", data={"text": "что-то"}, headers=HX)
    assert r.status_code == 200
    assert 'hx-swap-oob="true"' in r.text
    assert "Не разобрал" in r.text


async def test_done_drop_reopen_via_web(client, db, users, clock):
    t1 = await task(db, clock, users.a, "Первая")
    t2 = await task(db, clock, users.a, "Вторая")
    await login(client, db, users.a, clock)

    r = await client.post(f"/entries/{t1.id}/done", headers=HX)
    assert r.status_code == 200
    assert f'id="entry-{t1.id}"' in r.text
    assert "Первая" in r.text
    assert f"/entries/{t1.id}/reopen" in r.text
    assert "Первая" not in (await client.get("/tasks")).text

    r = await client.post(f"/entries/{t1.id}/reopen", headers=HX)
    assert f"/entries/{t1.id}/done" in r.text
    assert "Первая" in (await client.get("/tasks")).text

    r = await client.post(f"/entries/{t2.id}/drop", headers=HX)
    assert r.status_code == 200
    assert "Вторая" not in (await client.get("/tasks")).text


async def test_kind_section_share_actions(client, db, users, clock):
    t = await task(db, clock, users.a, "Меняем")
    sec = await get_or_create_section(db, users.a, "Работа")
    await login(client, db, users.a, clock)

    r = await client.post(f"/entries/{t.id}/section/{sec.id}", headers=HX)
    assert r.status_code == 200
    assert 'data-section="Работа"' in r.text
    r = await client.post(f"/entries/{t.id}/section/0", headers=HX)
    assert "data-section" not in r.text
    r = await client.post(f"/entries/{t.id}/share", data={"to": f"user:{users.b.id}"}, headers=HX)
    assert r.status_code == 200
    assert 'aria-label="Общая"' in r.text
    r = await client.post(f"/entries/{t.id}/kind/think", headers=HX)
    assert r.status_code == 200
    assert "Меняем" in (await client.get("/tasks")).text  # теперь в «Подумать»
    bad = await client.post(f"/entries/{t.id}/share", data={"to": "bad"}, headers=HX)
    assert bad.status_code == 404


async def test_action_on_hidden_entry_404_no_leak(client, db, users, clock):
    hidden = await task(db, clock, users.b, SECRET)
    await login(client, db, users.a, clock)
    for path, data in [
        (f"/entries/{hidden.id}/done", None),
        (f"/entries/{hidden.id}/drop", None),
        (f"/entries/{hidden.id}/reopen", None),
        (f"/entries/{hidden.id}/kind/note", None),
        (f"/entries/{hidden.id}/section/0", None),
        (f"/entries/{hidden.id}/share", {"to": f"user:{users.a.id}"}),
        (f"/entries/{hidden.id}/subtasks", None),
        ("/entries/99999/done", None),
    ]:
        r = await client.post(path, data=data, headers=HX)
        assert r.status_code == 404, path
        assert SECRET not in r.text
        assert "Нет доступа" in r.text


async def test_non_owner_cannot_drop_shared(client, db, users, clock):
    t = await task(db, clock, users.b, "Общая")
    await share(db, users.b, "entry", t.id, "user", users.a.id, clock)
    await login(client, db, users.a, clock)
    assert (await client.post(f"/entries/{t.id}/drop", headers=HX)).status_code == 404
    assert (await client.post(f"/entries/{t.id}/done", headers=HX)).status_code == 200


async def test_create_subtasks_twice_no_duplicates(client, db, users, clock):
    p = await create_entry(
        db,
        users.a,
        "project",
        "Проект",
        body=f"Текст\n\n{PROPOSAL_HEADER}\n- Раз\n- Два",
        clock=clock,
    )
    await login(client, db, users.a, clock)
    r = await client.post(f"/entries/{p.id}/subtasks", headers=HX)
    assert r.status_code == 200
    assert "0 из 2" in r.text
    r2 = await client.post(f"/entries/{p.id}/subtasks", headers=HX)
    assert r2.status_code == 200
    assert "Создать подзадачи" not in r2.text
    assert len(await children(db, users.a, p.id)) == 2


async def test_kind_project_proposes_then_creates_subtasks(client, db, users, clock, llm):
    t = await task(db, clock, users.a, "Ремонт кухни")
    llm.subtasks.append(["Замеры", "Закупка", "Монтаж"])
    await login(client, db, users.a, clock)
    r = await client.post(f"/entries/{t.id}/kind/project", headers=HX)
    assert r.status_code == 200
    assert "0 из 0" in r.text
    assert "Создать подзадачи" in r.text
    r = await client.post(f"/entries/{t.id}/subtasks", headers=HX)
    assert "0 из 3" in r.text
    assert "Создал подзадач: 3." in r.text
    assert "Создать подзадачи" not in r.text
    page = (await client.get("/tasks")).text
    assert "Проекты" in page
    assert "Ремонт кухни" in page
    assert "Замеры" in page
    done = await client.post(
        f"/entries/{(await children(db, users.a, t.id))[0].id}/done", headers=HX
    )
    assert done.status_code == 200
    assert "1 из 3" in (await client.get("/tasks")).text
    assert llm.calls[-1]["method"] == "decompose"
    assert sum(c["method"] == "decompose" for c in llm.calls) == 1


async def test_kind_project_decompose_failure_toast(client, db, users, clock, llm):
    t = await task(db, clock, users.a, "Что-то")
    llm.fail = True
    await login(client, db, users.a, clock)
    r = await client.post(f"/entries/{t.id}/kind/project", headers=HX)
    assert "Подзадачи предложить не удалось" in r.text
    assert 'hx-swap-oob="true"' in r.text


async def test_projects_group_order_and_filters(client, db, users, clock):
    await create_entry(db, users.a, "project", "МойПроект", clock=clock)
    await create_entry(db, users.b, "project", "ЧужойПроект", clock=clock)
    await create_entry(db, users.a, "think", "МояДума", clock=clock)
    await task(db, clock, users.a, "Без срока")
    await login(client, db, users.a, clock)
    page = (await client.get("/tasks")).text
    assert "МойПроект" in page
    assert "ЧужойПроект" not in page
    assert page.index("Без срока ·") < page.index("Проекты ·") < page.index("Подумать ·")
    assert "МойПроект" not in (await client.get("/tasks", params={"f": "shared"})).text


async def test_add_entry_always_returns_toast(client, db, users, clock, llm):
    llm.classifications.append(Classification("note", "Мысль", None, None, None))
    await login(client, db, users.a, clock)
    r = await client.post("/entries", data={"text": "мысль"}, headers=HX)
    assert 'hx-swap-oob="true"' in r.text
    assert "📌 Заметка: Мысль" in r.text


async def test_menu_hidden_for_non_owner_open_task(client, db, users, clock):
    t = await task(db, clock, users.b, "Чужая общая")
    await share(db, users.b, "entry", t.id, "user", users.a.id, clock)
    await login(client, db, users.a, clock)
    page = (await client.get("/tasks")).text
    assert "Чужая общая" in page
    assert "dropdown" not in page
    await login(client, db, users.b, clock)
    assert "dropdown" in (await client.get("/tasks")).text


async def test_non_owner_cannot_change_shared_entry(client, db, users, clock):
    t = await task(db, clock, users.b, "Общая")
    sec = await get_or_create_section(db, users.a, "Мой")
    await share(db, users.b, "entry", t.id, "user", users.a.id, clock)
    await login(client, db, users.a, clock)
    for path, data in [
        (f"/entries/{t.id}/kind/note", None),
        (f"/entries/{t.id}/section/{sec.id}", None),
        (f"/entries/{t.id}/share", {"to": f"user:{users.c.id}"}),
    ]:
        assert (await client.post(path, data=data, headers=HX)).status_code == 404, path
    row = await (
        await db.execute("SELECT kind, section_id FROM entries WHERE id = ?", (t.id,))
    ).fetchone()
    assert (row["kind"], row["section_id"]) == ("task", None)
    shares = await (await db.execute("SELECT COUNT(*) AS n FROM shares")).fetchone()
    assert shares["n"] == 1


async def test_share_to_unknown_user_404(client, db, users, clock):
    t = await task(db, clock, users.a, "Моя")
    await login(client, db, users.a, clock)
    r = await client.post(f"/entries/{t.id}/share", data={"to": "user:99999"}, headers=HX)
    assert r.status_code == 404


async def test_action_without_hx_request_403(client, db, users, clock):
    t = await task(db, clock, users.a, "Моя")
    await login(client, db, users.a, clock)
    assert (await client.post(f"/entries/{t.id}/done")).status_code == 403


def test_deadline_badge_scale():
    from hub.web.routes_pages import deadline_badge

    today = date(2026, 11, 2)
    assert deadline_badge(today - timedelta(days=1), today) == ("вчера", "error")
    assert deadline_badge(today - timedelta(days=5), today) == ("просрочено 5 дн.", "error")
    assert deadline_badge(today, today) == ("сегодня", "warning")
    assert deadline_badge(today + timedelta(days=3), today) == ("05.11", "warning")
    assert deadline_badge(today + timedelta(days=7), today) == ("09.11", "warning")
    assert deadline_badge(today + timedelta(days=8), today) == ("10.11", "neutral")
    assert deadline_badge(today + timedelta(days=40), today) == ("12.12", "neutral")
