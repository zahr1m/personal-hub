import hashlib
import hmac
import json
import re
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from hub.auth import create_login_link
from hub.crypto import SecretBox
from hub.web import create_app

TEMPLATES = Path(__file__).parent.parent / "hub" / "web" / "templates"


@pytest.fixture
def app(db, settings, clock):
    return create_app(
        db,
        service=None,  # type: ignore[arg-type]  # в Task 3 только кладётся в app.state
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


async def _login(client, db, user, clock) -> str:
    """Входит по ссылке; возвращает токен сессии из cookie."""
    link = await create_login_link(db, user, clock)
    r = await client.get("/auth/link", params={"t": link})
    assert r.status_code == 303
    return client.cookies["hub_session"]


def _init_data(bot_token: str, tg_id: int, auth_date: int) -> str:
    fields = {"auth_date": str(auth_date), "user": json.dumps({"id": tg_id})}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def test_healthz_open(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_pages_require_login_redirects_to_login_hint(client):
    r = await client.get("/")
    assert r.status_code == 401
    assert "Войдите: напишите боту /web" in r.text


async def test_htmx_401_has_reauth_trigger(client):
    r = await client.get("/", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers["HX-Trigger"] == "reauth"


async def test_link_login_sets_secure_cookie_and_redirects(client, db, users, clock):
    link = await create_login_link(db, users.a, clock)
    r = await client.get("/auth/link", params={"t": link})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert re.search(r"hub_session=[^;]+", cookie)
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", f"Max-Age={30 * 86400}"):
        assert flag.lower() in cookie.lower()
    page = await client.get("/")
    assert page.status_code == 200
    assert 'data-theme="hub-light"' in page.text
    assert users.a.name in page.text


async def test_used_link_shows_expired_page(client, db, users, clock):
    link = await create_login_link(db, users.a, clock)
    assert (await client.get("/auth/link", params={"t": link})).status_code == 303
    client.cookies.clear()
    r = await client.get("/auth/link", params={"t": link})
    assert r.status_code == 400
    assert "Ссылка устарела — попроси новую в боте /web" in r.text
    assert "hub_session" not in r.headers.get("set-cookie", "")


async def test_tma_login_returns_token(client, users, settings, clock):
    init = _init_data(settings.bot_token, users.a.tg_id, int(clock.now().timestamp()))
    r = await client.post("/auth/tma", json={"init_data": init})
    assert r.status_code == 200
    token = r.json()["token"]
    assert client.cookies["hub_session"] == token
    bad = await client.post("/auth/tma", json={"init_data": init + "x"})
    assert bad.status_code == 401


async def test_bearer_accepted_instead_of_cookie(client, db, users, clock):
    token = await _login(client, db, users.a, clock)
    client.cookies.clear()
    assert (await client.get("/")).status_code == 401
    ok = await client.get("/", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    # чужая схема авторизации не принимается
    basic = await client.get("/", headers={"Authorization": f"Basic {token}"})
    assert basic.status_code == 401


async def test_post_without_htmx_header_or_bearer_is_403(client, db, users, clock):
    token = await _login(client, db, users.a, clock)
    assert (await client.post("/logout")).status_code == 403
    # сессия при этом жива
    assert (await client.get("/")).status_code == 200
    bearer = await client.post("/logout", headers={"Authorization": f"Bearer {token}"})
    assert bearer.status_code == 204


async def test_security_headers_present(client):
    for path in ("/healthz", "/"):
        h = (await client.get(path)).headers
        csp = h["Content-Security-Policy"]
        assert "frame-ancestors 'self' https://web.telegram.org" in csp
        assert "script-src 'self' https://telegram.org" in csp
        assert "style-src 'self' https://fonts.googleapis.com" in csp
        assert "unsafe-inline" not in csp
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["Referrer-Policy"] == "no-referrer"


async def test_logout_revokes(client, db, users, clock):
    token = await _login(client, db, users.a, clock)
    r = await client.post("/logout", headers={"HX-Request": "true"})
    assert r.status_code == 204
    assert 'hub_session=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    client.cookies.clear()
    revoked = await client.get("/", headers={"Authorization": f"Bearer {token}"})
    assert revoked.status_code == 401


def test_templates_have_no_inline_styles():
    # CSP без unsafe-inline: ни style="…", ни <style>, ни hx-on
    files = list(TEMPLATES.glob("**/*.html"))
    assert files
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "style=" not in text, f
        assert "<style" not in text, f
        assert "hx-on" not in text, f


async def test_login_page_loads_telegram_script_before_app_js(client):
    r = await client.get("/")
    assert r.status_code == 401
    tg = r.text.index("https://telegram.org/js/telegram-web-app.js")
    assert tg < r.text.index("/static/app.js")
    assert "data-login" in r.text


async def test_unsafe_methods_blocked_by_default(app, client):
    @app.post("/_test_write")
    async def _write() -> dict[str, bool]:
        return {"ok": True}

    assert (await client.post("/_test_write")).status_code == 403
    assert (await client.delete("/_test_write")).status_code == 403
    ok = await client.post("/_test_write", headers={"HX-Request": "true"})
    assert ok.status_code == 200
    bearer = await client.post("/_test_write", headers={"Authorization": "Bearer x"})
    assert bearer.status_code == 200


async def test_500_response_has_security_headers(app):
    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://testserver",
    ) as c:
        r = await c.get("/boom")
    assert r.status_code == 500
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
