"""Настройки из переменных окружения / .env."""

from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    anthropic_api_key: str
    admin_tg_id: int
    db_path: Path = Path("/data/hub.db")
    backup_dir: Path = Path("/data/backups")
    brief_time: str = "09:00"
    daily_tokens_in: int = 2_000_000
    daily_tokens_out: int = 200_000
    model_fast: str = "claude-haiku-5-5"
    model_smart: str = "claude-sonnet-5-5"
    # пусто = регистрация по приглашению без ограничений; админ разрешён всегда
    web_base_url: str = "https://desk.example.com"
    web_port: int = 8080
    # ключ Fernet для секретов подключений (ссылки iCal)
    connections_key: str
    session_days: int = 30
    # имя бота без @: нужно для ссылки-приглашения; пусто — показываем только команду
    bot_username: str = ""
    allowed_tg_ids: Annotated[list[int], NoDecode] = []
    # имя из карты важнее имени из Telegram: «<ADMIN_TG_ID>=Аня,<USER2_TG_ID>=Боря»
    user_names: Annotated[dict[int, str], NoDecode] = {}

    @field_validator("web_base_url")
    @classmethod
    def _https_only(cls, v: str) -> str:
        # Telegram принимает WebApp только по https; http — лишь для локальной разработки
        if not v.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("web_base_url должен начинаться с https://")
        return v

    @field_validator("allowed_tg_ids", mode="before")
    @classmethod
    def _parse_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.split(",") if x.strip()]
        return v

    @field_validator("user_names", mode="before")
    @classmethod
    def _parse_names(cls, v: object) -> object:
        if isinstance(v, str):
            pairs = (x.split("=", 1) for x in v.split(",") if x.strip())
            return {int(k): name.strip() for k, name in pairs}
        return v
