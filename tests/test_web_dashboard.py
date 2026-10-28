from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from hub.access import share
from hub.auth import create_login_link
from hub.clock import FixedClock
from hub.connections import add_connection, disable_connection, set_status
from hub.crypto import SecretBox
from hub.entries import create_entry
from hub.llm import Classification
from hub.scheduler import run_brief
from hub.service import Service
from hub.users import create_invite, ensure_admin, redeem_invite
from hub.web import create_app
from tests.fakes import FakeLlm
from tests.test_web_settings import public

HX = {"HX-Request": "true"}
ICS = (Path(__file__).parent / "fixtures" / "calendar.ics").read_bytes()
URL = "https://calendar.example.com/private/TOPSECRET/basic.ics"
MSK = ZoneInfo("Europe/Moscow")
WIDGETS = ("brief", "calendar", "hot", "projects", "systems", "mail", "docs")


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
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=ICS))
        ),
        resolver=public,
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


async def task(db, clock, owner, title, due=None, kind="task", **kw):
    return await create_entry(db, owner, kind, title, due_date=due, clock=clock, **kw)


async def calendar(client, db, settings, clock, owner):
    conn = await add_connection(
        db, SecretBox(settings.connections_key), owner, "ics_calendar", "Рабочий", URL, "x", clock
    )
    await login(client, db, owner, clock)
    assert (await client.post(f"/connections/{conn.id}/check", headers=HX)).status_code == 200
    return conn


async def test_dashboard_has_widgets_and_kpis(client, db, users, clock):
    today = clock.today()
    await task(db, clock, users.a, "Купить билеты", today - timedelta(days=1))
    await task(db, clock, users.a, "Позвонить в УК", today)
    await task(db, clock, users.a, "Отпуск", kind="project")
    await login(client, db, users.a, clock)
    page = await client.get("/")
    assert page.status_code == 200
    for name in (*WIDGETS, "kpis"):
        assert f'hx-get="/widgets/{name}"' in page.text
    assert "xl:grid-cols-3" in page.text
    assert "md:grid-cols-2" in page.text
    assert "style=" not in page.text
    assert "hx-on" not in page.text
    kpis = (await client.get("/widgets/kpis")).text
    for label in ("Просрочено", "Купить билеты", "На сегодня", "Почта", "Проекты"):
        assert label in kpis


@pytest.mark.parametrize(
    ("hour", "greeting"), [(8, "Доброе утро"), (14, "День добрый"), (20, "Добрый вечер")]
)
async def test_dashboard_greeting_by_time(client, app, db, users, clock, hour, greeting):
    app.state.clock = FixedClock(datetime(2026, 11, 2, hour, 0, tzinfo=MSK))
    await login(client, db, users.a, app.state.clock)
    page = (await client.get("/")).text
    assert f"{greeting}, {users.a.name}" in page
    assert "Понедельник, 2 ноября" in page


async def test_hot_widget_shows_overdue_and_today_only_visible(client, db, users, clock):
    today = clock.today()
    await task(db, clock, users.a, "Просрочка", today - timedelta(days=2))
    await task(db, clock, users.a, "Сегодняшняя", today)
    await task(db, clock, users.a, "Потом", today + timedelta(days=3))
    await task(db, clock, users.b, "ЧужаяЛичная", today)
    theirs = await task(db, clock, users.b, "ЧужаяОбщая", today)
    await share(db, users.b, "entry", theirs.id, "user", users.a.id, clock)
    await login(client, db, users.a, clock)
    text = (await client.get("/widgets/hot")).text
    assert "Просрочка" in text
    assert "Сегодняшняя" in text
    assert "ЧужаяОбщая" in text
    assert "Потом" not in text
    assert "ЧужаяЛичная" not in text
    assert "/done" in text  # кнопка «выполнено»


async def test_hot_widget_empty_and_limit(client, db, users, clock):
    await login(client, db, users.a, clock)
    assert "Ничего не горит" in (await client.get("/widgets/hot")).text
    for i in range(7):
        await task(db, clock, users.a, f"Горит{i}", clock.today())
    text = (await client.get("/widgets/hot")).text
    assert sum(f"Горит{i}" in text for i in range(7)) == 5


async def test_calendar_widget_three_days_and_empty_state(client, db, settings, users, clock):
    await calendar(client, db, settings, clock, users.a)
    text = (await client.get("/widgets/calendar")).text
    for label in ("Сегодня", "Завтра, вт", "Среда"):
        assert label in text
    assert "Разовая встреча" in text
    assert "весь день · День рождения" in text
    assert "Подключить календарь" not in text

    await login(client, db, users.b, clock)  # чужой календарь не виден
    assert "Разовая встреча" not in (await client.get("/widgets/calendar")).text


async def test_projects_widget_progress(client, db, users, clock):
    p = await task(db, clock, users.a, "Ремонт", kind="project")
    done = await task(db, clock, users.a, "Плитка", parent_id=p.id)
    await task(db, clock, users.a, "Кран", parent_id=p.id)
    await login(client, db, users.a, clock)
    assert (await client.post(f"/entries/{done.id}/done", headers=HX)).status_code == 200
    text = (await client.get("/widgets/projects")).text
    assert "Ремонт" in text
    assert "1 из 2" in text
    assert 'value="1" max="2"' in text


async def test_shared_widget_titled_by_partner_and_hidden_when_alone(db, clock, settings):
    admin = await ensure_admin(db, 1, "Аня", clock)
    app = create_app(
        db,
        service=Service(db, FakeLlm(), clock, settings),
        settings=settings,
        clock=clock,
        box=SecretBox(settings.connections_key),
        http_client_factory=httpx.AsyncClient,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        await login(client, db, admin, clock)
        alone = (await client.get("/")).text
        assert 'hx-get="/widgets/shared"' not in alone
        assert (await client.get("/widgets/shared")).text.strip() == ""

        partner = await redeem_invite(
            db, await create_invite(db, admin, clock), 2, "Боря", clock
        )
        mine = await task(db, clock, admin, "Выбрать плитку")
        await task(db, clock, admin, "Личное")
        await share(db, admin, "entry", mine.id, "user", partner.id, clock)
        page = (await client.get("/")).text
        assert 'hx-get="/widgets/shared"' in page
        # общее обновляется и по kpi-changed, а не только раз в 10 минут
        shared = page.split("/widgets/shared")[1][:200]
        assert "entries-changed from:body, kpi-changed from:body" in shared
        text = (await client.get("/widgets/shared")).text
        assert "Боря · общее" in text
        assert "Выбрать плитку" in text
        assert "Личное" not in text


async def test_systems_widget_statuses(client, db, settings, users, clock):
    box = SecretBox(settings.connections_key)
    ok = await add_connection(db, box, users.a, "ics_calendar", "Рабочий", URL, "x", clock)
    bad = await add_connection(db, box, users.a, "ics_calendar", "Личный", URL, "y", clock)
    await add_connection(db, box, users.b, "ics_calendar", "ЧужойКал", URL, "z", clock)
    await set_status(db, ok.id, "ok", None, clock, success=True)
    await set_status(db, bad.id, "error", "ссылка недоступна", clock, success=False)
    await login(client, db, users.a, clock)
    text = (await client.get("/widgets/systems")).text
    assert "Бот" in text
    assert "Рабочий" in text
    assert "Работает" in text
    assert "Ошибка: ссылка недоступна" in text
    assert 'href="/settings"' in text
    assert "ЧужойКал" not in text


async def test_brief_widget_shows_saved_intro_without_llm(client, db, users, clock, llm):
    await login(client, db, users.a, clock)
    fresh = (await client.get("/widgets/brief")).text
    assert 'hx-post="/brief"' in fresh
    llm.intros.append("Сегодня лёгкий день")
    out = []

    async def send(tg_id, text):
        out.append(text)

    assert await run_brief(db, llm, send, users.a, clock.today()) == "sent"
    calls = len(llm.calls)
    sent = (await client.get("/widgets/brief")).text
    assert "Сегодня лёгкий день" in sent
    assert 'hx-post="/brief"' not in sent  # вступление есть - кнопки нет
    assert len(llm.calls) == calls


async def test_brief_widget_raw_fallback_and_other_user_intro_hidden(client, db, users, clock, llm):
    async def send(tg_id, text):
        return None

    llm.intros.append("Секрет Б")
    await run_brief(db, llm, send, users.b, clock.today())
    llm.fail = True
    await run_brief(db, llm, send, users.a, clock.today(), final_attempt=True)
    await login(client, db, users.a, clock)
    text = (await client.get("/widgets/brief")).text
    assert "Секрет Б" not in text
    assert "без вступления" in text
    assert 'hx-post="/brief"' in text


async def test_brief_button_builds_intro_only(client, db, users, clock, llm):
    llm.intros.append("Сегодня спокойный день.")
    await task(db, clock, users.a, "Горящая", clock.today())
    await login(client, db, users.a, clock)
    r = await client.post("/brief", headers=HX)
    assert r.status_code == 200
    assert "Сегодня спокойный день." in r.text
    assert "Горящая" not in r.text
    assert 'hx-post="/brief"' not in r.text
    assert [c["method"] for c in llm.calls] == ["brief_intro"]
    row = await (
        await db.execute("SELECT * FROM briefs WHERE user_id = ?", (users.a.id,))
    ).fetchall()
    assert row == []  # кнопка статус не пишет


async def test_dashboard_requires_login_and_hx(client, db, users, clock):
    for name in ("kpis", "brief", "calendar", "hot"):
        assert (await client.get(f"/widgets/{name}")).status_code == 401
    assert (await client.post("/brief", headers=HX)).status_code == 401
    await login(client, db, users.a, clock)
    assert (await client.post("/brief")).status_code == 403


async def test_done_in_hot_swaps_row_with_undo_and_refreshes_only_tiles(client, db, users, clock):
    t = await task(db, clock, users.a, "Горящая", clock.today())
    await login(client, db, users.a, clock)
    r = await client.post(f"/entries/{t.id}/done", headers=HX)
    assert r.headers["HX-Trigger"] == "kpi-changed"
    assert f"/entries/{t.id}/reopen" in r.text  # «вернуть» в той же строке
    page = (await client.get("/")).text
    kpis = page[page.index('id="w-kpis"') :].split(">", 1)[0]
    hot = page[page.index('id="w-hot"') :].split(">", 1)[0]
    assert "kpi-changed from:body" in kpis
    assert "kpi-changed" not in hot
    assert "entries-changed from:body" in hot


async def test_brief_button_result_persists_across_reload(client, db, users, clock, llm):
    llm.intros.append("Ручное вступление")
    await login(client, db, users.a, clock)
    assert "Ручное вступление" in (await client.post("/brief", headers=HX)).text
    calls = len(llm.calls)
    again = (await client.get("/widgets/brief")).text
    assert "Ручное вступление" in again
    assert 'hx-post="/brief"' not in again
    assert len(llm.calls) == calls
    await login(client, db, users.b, clock)
    other = (await client.get("/widgets/brief")).text
    assert "Ручное вступление" not in other
    assert 'hx-post="/brief"' in other


async def test_morning_intro_wins_over_manual(client, db, users, clock, llm):
    async def send(tg_id, text):
        return None

    llm.intros.extend(["Ручное", "Утреннее"])
    await login(client, db, users.a, clock)
    await client.post("/brief", headers=HX)
    await run_brief(db, llm, send, users.a, clock.today())
    text = (await client.get("/widgets/brief")).text
    assert "Утреннее" in text
    assert "Ручное" not in text


@pytest.mark.parametrize("status", ["error", "auth_expired"])
async def test_calendar_broken_connection_shows_warning(client, db, settings, users, clock, status):
    conn = await add_connection(
        db, SecretBox(settings.connections_key), users.a, "ics_calendar", "К", URL, "x", clock
    )
    await set_status(db, conn.id, status, "сбой", clock, success=False)
    await login(client, db, users.a, clock)
    text = (await client.get("/widgets/calendar")).text
    assert "Календарь не обновляется" in text
    assert 'href="/settings"' in text
    assert "нет событий" in text


async def test_calendar_disabled_old_plus_healthy_new_has_no_warning(
    client, db, settings, users, clock
):
    box = SecretBox(settings.connections_key)
    old = await add_connection(db, box, users.a, "ics_calendar", "Старый", URL, "x", clock)
    await set_status(db, old.id, "error", "сбой", clock, success=False)
    await disable_connection(db, users.a, old.id, clock)
    await calendar(client, db, settings, clock, users.a)
    text = (await client.get("/widgets/calendar")).text
    assert "не обновляется" not in text
    assert "Календарь не подключён" not in text


async def test_calendar_all_disabled_shows_not_connected(client, db, settings, users, clock):
    conn = await add_connection(
        db, SecretBox(settings.connections_key), users.a, "ics_calendar", "К", URL, "x", clock
    )
    await set_status(db, conn.id, "error", "сбой", clock, success=False)
    await disable_connection(db, users.a, conn.id, clock)
    await login(client, db, users.a, clock)
    text = (await client.get("/widgets/calendar")).text
    assert "Календарь не подключён" in text
    assert "не обновляется" not in text
    assert "нет событий" not in text


async def test_calendar_healthy_connection_has_no_warning(client, db, settings, users, clock):
    await calendar(client, db, settings, clock, users.a)
    assert "не обновляется" not in (await client.get("/widgets/calendar")).text


async def test_calendar_not_connected_shows_only_link(client, db, users, clock):
    await login(client, db, users.b, clock)
    text = (await client.get("/widgets/calendar")).text
    assert "Календарь не подключён" in text
    assert "Подключить календарь" in text
    assert "нет событий" not in text
    assert "Завтра" not in text


async def test_calendar_midnight_event_shows_until_time(client, db, settings, users, clock):
    conn = await add_connection(
        db, SecretBox(settings.connections_key), users.a, "ics_calendar", "К", URL, "x", clock
    )
    await db.execute(
        "INSERT INTO calendar_events(connection_id, uid, starts_at, ends_at, all_day, title)"
        " VALUES (?, 'n', '2026-11-01T23:00:00+03:00', '2026-11-02T01:30:00+03:00', 0, 'Поезд')",
        (conn.id,),
    )
    await db.commit()
    await login(client, db, users.a, clock)
    text = (await client.get("/widgets/calendar")).text
    assert "до 01:30" in text
    assert "Поезд" in text
    assert "весь день · Поезд" not in text
    assert "нет событий" in text  # завтра и послезавтра пусты


async def test_quick_add_triggers_entries_changed(client, db, users, clock, llm):
    llm.classifications.append(Classification("task", "Купить хлеб", None, None, None))
    await login(client, db, users.a, clock)
    r = await client.post("/entries", data={"text": "купить хлеб", "view": "dash"}, headers=HX)
    assert r.status_code == 200
    assert r.headers["HX-Trigger"] == "entries-changed"
    assert 'hx-swap-oob="true"' in r.text
    assert "Купить хлеб" in r.text


async def test_stub_pages(client, db, users, clock):
    await login(client, db, users.a, clock)
    for path in ("/mail", "/docs", "/calendar"):
        r = await client.get(path)
        assert r.status_code == 200
        assert "Появится позже" in r.text
    menu = (await client.get("/")).text
    for label in ("Главная", "Задачи", "Календарь", "Почта", "Документы"):
        assert label in menu
