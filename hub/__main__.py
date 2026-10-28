"""Точка входа: long polling, веб-сервер и планировщик в одном event loop."""

import asyncio
import contextlib
import json
import logging
import sys
from datetime import UTC, datetime

import httpx
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from hub.bot import build_router
from hub.clock import Clock
from hub.config import Settings
from hub.crypto import SecretBox
from hub.db import open_db
from hub.llm import AnthropicLlm
from hub.models import User
from hub.scheduler import build_scheduler, schedule_calendar_sync, schedule_user
from hub.service import Service
from hub.users import ensure_admin, list_users, rename_users
from hub.web import create_app

ADMIN_NAME = "Админ"


class JsonFormatter(logging.Formatter):
    """Одна строка JSON на запись; текстов сообщений пользователей в логах нет."""

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # httpx пишет полный URL запроса, а ссылки iCal — секрет
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class WebServer(uvicorn.Server):
    """Сигналы (SIGTERM/SIGINT) обрабатывает aiogram, uvicorn их не перехватывает."""

    def capture_signals(self):
        return contextlib.nullcontext()


def make_server(app, port: int) -> uvicorn.Server:
    # логи доступа выключены: в URL ссылки входа лежит токен
    return WebServer(
        uvicorn.Config(
            app,
            host="0.0.0.0",  # noqa: S104 — в контейнере за nginx
            port=port,
            proxy_headers=True,
            forwarded_allow_ips="*",
            log_config=None,
            access_log=False,
        )
    )


async def run_together(dp: Dispatcher, bot: Bot, server: uvicorn.Server) -> None:
    """Поллинг и веб вместе: остановка одного останавливает второе, сбой не теряется."""

    async def poll() -> None:
        try:
            await dp.start_polling(bot)  # SIGTERM/SIGINT ловит aiogram
        finally:
            server.should_exit = True

    async def serve() -> None:
        try:
            await server.serve()
        finally:
            with contextlib.suppress(RuntimeError):  # поллинг уже остановлен
                await dp.stop_polling()

    results = await asyncio.gather(poll(), serve(), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


def apply_bot_username(settings: Settings, username: str) -> str:
    """Имя бота из get_me() нужно и вебу (ссылка-приглашение); .env имеет приоритет."""
    settings.bot_username = settings.bot_username or username
    return settings.bot_username


async def main() -> None:
    setup_logging()
    settings = Settings()
    clock = Clock()
    db = await open_db(settings.db_path)
    scheduler = bot = llm = None
    try:
        admin = await ensure_admin(db, settings.admin_tg_id, ADMIN_NAME, clock, settings.user_names)
        await rename_users(db, settings.user_names)
        llm = AnthropicLlm(settings, db, clock)
        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=None))

        async def send(tg_id: int, text: str) -> None:
            await bot.send_message(tg_id, text)

        scheduler = build_scheduler(db, llm, send, settings, admin, await list_users(db))

        async def on_register(user: User) -> None:
            schedule_user(scheduler, db, llm, send, user, admin)

        async def on_brief_time_changed(user: User) -> None:
            schedule_user(scheduler, db, llm, send, user, admin)

        def http_client_factory() -> httpx.AsyncClient:
            # без редиректов и прокси из окружения: адрес проверяется до запроса
            return httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)

        box = SecretBox(settings.connections_key)
        schedule_calendar_sync(scheduler, db, box, http_client_factory, clock)
        service = Service(db, llm, clock, settings, on_register=on_register)
        bot_username = apply_bot_username(settings, (await bot.get_me()).username)
        dp = Dispatcher()
        dp.include_router(build_router(service, bot_username))
        app = create_app(
            db, service, settings, clock, box, http_client_factory, on_brief_time_changed
        )
        server = make_server(app, settings.web_port)

        scheduler.start()
        await run_together(dp, bot, server)
    finally:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
        if bot is not None:
            await bot.session.close()
        if llm is not None:
            await llm.aclose()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
