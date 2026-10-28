import asyncio
import logging
import signal
import subprocess
import sys
import textwrap
import threading

import httpx
import pytest

from hub.__main__ import make_server, run_together, setup_logging
from hub.calendar_ics import fetch_ics
from tests.test_calendar_ics import PUBLIC


async def test_calendar_url_not_logged(caplog):
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    try:
        setup_logging()
        root.addHandler(caplog.handler)  # setup_logging убрал обработчик caplog
        url = "https://x.example.com/secret-path/cal.ics"
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x"))
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            await fetch_ics(url, client, PUBLIC)
        assert "secret-path" not in caplog.text
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


def test_server_has_no_access_log_and_leaves_signals_alone():
    server = make_server(lambda *a: None, 0)
    assert server.config.access_log is False
    before = signal.getsignal(signal.SIGINT)
    with server.capture_signals():
        assert signal.getsignal(signal.SIGINT) is before


class FakeServer:
    def __init__(self, finish_after=None):
        self.should_exit = False
        self.finish_after = finish_after

    async def serve(self):
        while not self.should_exit:
            if self.finish_after is not None:
                await asyncio.sleep(self.finish_after)
                return
            await asyncio.sleep(0.01)


class FakeDp:
    def __init__(self, finish_after=None):
        self.stopped = False
        self.finish_after = finish_after

    async def start_polling(self, bot):
        while not self.stopped:
            if self.finish_after is not None:
                await asyncio.sleep(self.finish_after)
                return
            await asyncio.sleep(0.01)

    async def stop_polling(self):
        self.stopped = True


async def test_polling_ends_then_server_exits():
    server = FakeServer()
    await asyncio.wait_for(run_together(FakeDp(finish_after=0.02), None, server), 2)
    assert server.should_exit is True


async def test_server_ends_then_polling_stops():
    dp = FakeDp()
    await asyncio.wait_for(run_together(dp, None, FakeServer(finish_after=0.02)), 2)
    assert dp.stopped is True


async def test_failure_is_not_lost_and_other_side_stops():
    class Boom(FakeDp):
        async def start_polling(self, bot):
            raise RuntimeError("boom")

    server = FakeServer()
    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(run_together(Boom(), None, server), 2)
    assert server.should_exit is True


SCRIPT = textwrap.dedent(
    """
    import asyncio
    import signal
    from aiogram import Bot, Dispatcher
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.methods import GetMe, GetUpdates
    from aiogram.types import User
    from hub.__main__ import make_server, run_together

    class Session(AiohttpSession):
        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, GetMe):
                return User(id=1, is_bot=True, first_name="b", username="b")
            if isinstance(method, GetUpdates):
                await asyncio.sleep(0.2)
                return []
            return True

    async def app(scope, receive, send):
        pass

    async def announce(server):
        # готов, когда сервер слушает порт, а aiogram перехватил SIGTERM
        while not (server.started and signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL):
            await asyncio.sleep(0.05)
        print("ready", flush=True)

    async def main():
        bot = Bot("1:abc", session=Session())
        server = make_server(app, 0)
        watcher = asyncio.create_task(announce(server))
        try:
            await run_together(Dispatcher(), bot, server)
        finally:
            watcher.cancel()
            print("cleanup", flush=True)

    asyncio.run(main())
    """
)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SIGTERM как в Docker — Linux")
def test_sigterm_runs_cleanup_and_exits_zero():
    proc = subprocess.Popen([sys.executable, "-c", SCRIPT], stdout=subprocess.PIPE, text=True)
    guard = threading.Timer(60, proc.kill)  # страховка от зависшего старта
    guard.start()
    try:
        assert proc.stdout.readline().strip() == "ready"
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=15)
    finally:
        guard.cancel()
    assert "cleanup" in out
    assert proc.returncode == 0


async def test_bot_username_from_get_me_reaches_web_invite(db, settings, clock):
    # стык: имя из get_me() попадает в веб-приглашение (без сети)

    from cryptography.fernet import Fernet

    from hub.__main__ import apply_bot_username
    from hub.auth import create_login_link
    from hub.crypto import SecretBox
    from hub.service import Service
    from hub.users import ensure_admin
    from hub.web import create_app
    from tests.fakes import FakeLlm

    assert settings.bot_username == ""
    assert apply_bot_username(settings, "hub_bot") == "hub_bot"
    admin = await ensure_admin(db, 1, "Админ", clock, {})
    app = create_app(
        db,
        Service(db, FakeLlm(), clock, settings),
        settings,
        clock,
        SecretBox(Fernet.generate_key().decode()),
        lambda: httpx.AsyncClient(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as c:
        link = await create_login_link(db, admin, clock)
        assert (await c.get("/auth/link", params={"t": link})).status_code == 303
        resp = await c.post("/invites", headers={"HX-Request": "true"})
    assert "https://t.me/hub_bot?start=" in resp.text


def test_apply_bot_username_keeps_configured(settings):
    from hub.__main__ import apply_bot_username

    settings.bot_username = "custom_bot"
    assert apply_bot_username(settings, "hub_bot") == "custom_bot"
    assert settings.bot_username == "custom_bot"
