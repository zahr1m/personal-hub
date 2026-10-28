import socket
from datetime import date
from pathlib import Path

import httpx
import pytest

from hub.access import share
from hub.auth import create_login_link
from hub.budget import record_usage
from hub.calendar_ics import events_for_day
from hub.connections import add_connection, list_connections
from hub.crypto import SecretBox
from hub.entries import create_entry
from hub.service import Service
from hub.users import get_user_by_id
from hub.web import create_app
from tests.fakes import FakeLlm

HX = {"HX-Request": "true"}
ICS = (Path(__file__).parent / "fixtures" / "calendar.ics").read_bytes()
URL = "https://calendar.example.com/private/TOPSECRET/basic.ics"


async def public(host, port):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


async def private(host, port):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]


class Env:
    """Подменяемые снаружи ответ календаря и обратный вызов смены времени."""

    def __init__(self):
        self.handler = lambda request: httpx.Response(200, content=ICS)
        self.changed = []


@pytest.fixture
def env():
    return Env()


@pytest.fixture
def app(db, settings, clock, env):
    settings.bot_username = "hub_bot"

    async def on_changed(user):
        env.changed.append(user)

    return create_app(
        db,
        service=Service(db, FakeLlm(), clock, settings),
        settings=settings,
        clock=clock,
        box=SecretBox(settings.connections_key),
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(env.handler)),
        on_brief_time_changed=on_changed,
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


async def connect(db, settings, clock, owner, name, url=URL, host="calendar.example.com"):
    return await add_connection(
        db, SecretBox(settings.connections_key), owner, "ics_calendar", name, url, host, clock
    )


async def test_settings_lists_only_own_connections(client, db, settings, users, clock):
    await connect(db, settings, clock, users.a, "Рабочий")
    await connect(db, settings, clock, users.b, "Чужой", host="other.example.org")
    await login(client, db, users.a, clock)
    page = (await client.get("/settings")).text
    assert "Рабочий" in page
    assert "calendar.example.com" in page
    assert "Чужой" not in page
    assert "other.example.org" not in page
    assert "Ждёт первой синхронизации" in page


async def test_add_calendar_unsafe_url_rejected_nothing_saved(client, app, db, users, clock):
    app.state.resolver = private
    await login(client, db, users.a, clock)
    resp = await client.post("/connections", data={"name": "Рабочий", "url": URL}, headers=HX)
    assert resp.status_code == 200
    assert "внутренний адрес" in resp.text
    assert "TOPSECRET" not in resp.text
    assert await list_connections(db, users.a) == []


async def test_add_calendar_rejects_bad_name_and_http(client, db, users, clock):
    await login(client, db, users.a, clock)
    for data in (
        {"name": "  ", "url": URL},
        {"name": "x" * 61, "url": URL},
        {"name": "Ок", "url": URL.replace("https", "http")},
    ):
        resp = await client.post("/connections", data=data, headers=HX)
        assert resp.status_code == 200
        assert "alert-error" in resp.text
    assert await list_connections(db, users.a) == []


async def test_add_calendar_fetch_error_shown_nothing_saved(client, env, db, users, clock):
    env.handler = lambda request: httpx.Response(500)
    await login(client, db, users.a, clock)
    resp = await client.post("/connections", data={"name": "К", "url": URL}, headers=HX)
    assert "ошибкой 500" in resp.text
    assert await list_connections(db, users.a) == []


async def test_add_calendar_ok_saves_and_syncs(client, db, users, clock):
    await login(client, db, users.a, clock)
    resp = await client.post("/connections", data={"name": "Рабочий", "url": URL}, headers=HX)
    assert resp.status_code == 200
    (conn,) = await list_connections(db, users.a)
    assert (conn.name, conn.hint, conn.status) == ("Рабочий", "calendar.example.com", "ok")
    assert "Работает" in resp.text
    assert "TOPSECRET" not in resp.text
    assert URL not in resp.text
    events = await events_for_day(db, users.a, date(2026, 11, 3))
    assert [e.title for e in events] == ["Разовая встреча"]


async def test_secret_never_rendered(client, db, users, clock):
    await login(client, db, users.a, clock)
    added = (await client.post("/connections", data={"name": "К", "url": URL}, headers=HX)).text
    (conn,) = await list_connections(db, users.a)
    page = (await client.get("/settings")).text
    checked = (await client.post(f"/connections/{conn.id}/check", headers=HX)).text
    for html in (added, page, checked):
        assert URL not in html
        assert "TOPSECRET" not in html
        assert "/private/" not in html
        assert "calendar.example.com" in html


async def test_check_connection_updates_status(client, env, db, settings, users, clock):
    conn = await connect(db, settings, clock, users.a, "Рабочий")
    await login(client, db, users.a, clock)
    resp = await client.post(f"/connections/{conn.id}/check", headers=HX)
    assert resp.status_code == 200
    assert "Работает" in resp.text
    assert "только что" in resp.text
    env.handler = lambda request: httpx.Response(500)
    resp = await client.post(f"/connections/{conn.id}/check", headers=HX)
    assert "Ошибка: календарь ответил ошибкой 500" in resp.text


async def test_disable_connection(client, db, settings, users, clock):
    conn = await connect(db, settings, clock, users.a, "Рабочий")
    await login(client, db, users.a, clock)
    resp = await client.post(f"/connections/{conn.id}/disable", headers=HX)
    assert "Отключено" in resp.text
    assert "Проверить" not in resp.text
    (after,) = await list_connections(db, users.a)
    assert after.status == "disabled"


async def test_check_connection_other_user_404(client, db, settings, users, clock):
    theirs = await connect(db, settings, clock, users.b, "Чужой")
    await login(client, db, users.a, clock)
    assert (await client.post(f"/connections/{theirs.id}/check", headers=HX)).status_code == 404
    assert (await client.post(f"/connections/{theirs.id}/disable", headers=HX)).status_code == 404
    assert (await client.post("/connections/9999/check", headers=HX)).status_code == 404
    (still,) = await list_connections(db, users.b)
    assert still.status == "pending"


async def test_brief_time_validation_and_reschedule_callback(client, env, db, users, clock):
    await login(client, db, users.a, clock)
    for bad in ("04:59", "12:01", "9:00", "abc", "25:00", ""):
        resp = await client.post("/settings/brief-time", data={"brief_time": bad}, headers=HX)
        assert resp.status_code == 200
        assert "alert-error" in resp.text
    assert env.changed == []
    assert (await get_user_by_id(db, users.a.id)).brief_time == "09:00"
    for ok in ("05:00", "12:00", "08:30"):
        resp = await client.post("/settings/brief-time", data={"brief_time": ok}, headers=HX)
        assert "alert-error" not in resp.text
    assert (await get_user_by_id(db, users.a.id)).brief_time == "08:30"
    assert [u.brief_time for u in env.changed] == ["05:00", "12:00", "08:30"]
    assert all(u.id == users.a.id for u in env.changed)


async def test_admin_sees_users_and_invite_user_does_not(client, db, users, clock):
    await login(client, db, users.a, clock)
    page = (await client.get("/settings")).text
    assert "Пригласить" in page
    assert users.b.name in page
    resp = await client.post("/invites", headers=HX)
    assert resp.status_code == 200
    assert "/start " in resp.text
    assert "https://t.me/hub_bot?start=" in resp.text

    await login(client, db, users.b, clock)
    page = (await client.get("/settings")).text
    assert "Пригласить" not in page
    assert "Пользователи" not in page
    assert (await client.post("/invites", headers=HX)).status_code == 404


async def test_invite_without_bot_username_shows_code_only(client, app, db, users, clock):
    app.state.settings.bot_username = ""
    await login(client, db, users.a, clock)
    resp = await client.post("/invites", headers=HX)
    assert "/start " in resp.text
    assert "t.me" not in resp.text
    assert "отправь боту" in resp.text


async def test_usage_and_sharing_blocks(client, db, users, clock):
    day = clock.today()
    await record_usage(db, users.a.id, day, 18_200, 4_100)
    await record_usage(db, users.b.id, day, 777, 55)
    mine = await create_entry(db, users.a, "task", "Моя", clock=clock)
    await share(db, users.a, "entry", mine.id, "user", users.b.id, clock)
    theirs = await create_entry(db, users.b, "task", "Их", clock=clock)
    await share(db, users.b, "entry", theirs.id, "user", users.a.id, clock)
    await login(client, db, users.b, clock)
    page = (await client.get("/settings")).text
    assert "777" in page
    assert "18 200" not in page  # чужой расход обычному пользователю не виден
    await login(client, db, users.a, clock)
    page = (await client.get("/settings")).text
    assert "18 200" in page
    assert "777" in page  # админ видит всех
    assert 'data-shared-by-me="1"' in page
    assert 'data-shared-with-me="1"' in page


@pytest.mark.parametrize(
    "bad",
    [
        "https://.example.com/TOPSECRET/a.ics",
        "https://xn--zz.com/TOPSECRET/a.ics",
        "https://" + "a" * 70 + ".com/TOPSECRET/a.ics",
        "https://calendar.example.com/TOP\tSECRET/a.ics",
        "https://calendar.example.com/TOP\x00SECRET/a.ics",
    ],
)
async def test_add_calendar_malformed_url_is_form_error_not_500(client, app, db, users, clock, bad):
    async def idna_resolver(host, port):
        host.encode("idna")  # как настоящий getaddrinfo: UnicodeError на кривом хосте
        return await public(host, port)

    app.state.resolver = idna_resolver
    await login(client, db, users.a, clock)
    resp = await client.post("/connections", data={"name": "К", "url": bad}, headers=HX)
    assert resp.status_code == 200
    assert "alert-error" in resp.text
    assert "TOPSECRET" not in resp.text
    assert await list_connections(db, users.a) == []


@pytest.mark.parametrize("field", ["daily_tokens_in", "daily_tokens_out"])
async def test_settings_zero_token_limit_shows_not_set(client, app, db, users, clock, field):
    setattr(app.state.settings, field, 0)
    await record_usage(db, users.a.id, clock.today(), 5, 5)
    await login(client, db, users.a, clock)
    resp = await client.get("/settings")
    assert resp.status_code == 200
    assert "лимит не задан" in resp.text
