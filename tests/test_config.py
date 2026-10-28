import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from hub.clock import Clock, FixedClock
from hub.config import Settings

KEY = Fernet.generate_key().decode()


def test_settings_defaults(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ADMIN_TG_ID", "1")
    monkeypatch.setenv("CONNECTIONS_KEY", KEY)
    s = Settings()
    assert (s.daily_tokens_in, s.daily_tokens_out, s.brief_time) == (2_000_000, 200_000, "09:00")
    assert (s.allowed_tg_ids, s.user_names) == ([], {})


def test_system_tz_env_is_not_read(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ADMIN_TG_ID", "1")
    monkeypatch.setenv("CONNECTIONS_KEY", KEY)
    monkeypatch.setenv("TZ", "UTC")
    assert not hasattr(Settings(), "tz")


def test_allowed_ids_and_names_from_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ADMIN_TG_ID", "1")
    monkeypatch.setenv("CONNECTIONS_KEY", KEY)
    monkeypatch.setenv("ALLOWED_TG_IDS", "10, 20,30")
    monkeypatch.setenv("USER_NAMES", "10=Аня,20=Боря")
    s = Settings()
    assert s.allowed_tg_ids == [10, 20, 30]
    assert s.user_names == {10: "Аня", 20: "Боря"}


def test_empty_allowed_ids_and_names(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ADMIN_TG_ID", "1")
    monkeypatch.setenv("CONNECTIONS_KEY", KEY)
    monkeypatch.setenv("ALLOWED_TG_IDS", "")
    monkeypatch.setenv("USER_NAMES", "")
    s = Settings()
    assert (s.allowed_tg_ids, s.user_names) == ([], {})


def test_clock_is_moscow_aware():
    now = Clock().now()
    assert now.utcoffset().total_seconds() == 3 * 3600
    assert Clock().today() == now.date()


def test_fixed_clock(clock):
    assert clock.today().isoformat() == "2026-11-02"
    assert clock.today().weekday() == 0
    assert isinstance(clock, FixedClock)


def test_settings_requires_connections_key(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ADMIN_TG_ID", "1")
    monkeypatch.delenv("CONNECTIONS_KEY", raising=False)
    with pytest.raises(ValidationError, match="connections_key"):
        Settings(_env_file=None)


def test_web_defaults(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ADMIN_TG_ID", "1")
    monkeypatch.setenv("CONNECTIONS_KEY", KEY)
    s = Settings()
    assert (s.web_base_url, s.web_port, s.session_days) == ("https://desk.example.com", 8080, 30)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://desk.example.com", True),
        ("http://localhost:8080", True),
        ("http://127.0.0.1:8080", True),
        ("http://desk.example.com", False),
        ("desk.example.com", False),
    ],
)
def test_web_base_url_must_be_https(monkeypatch, url, ok):
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ADMIN_TG_ID", "1")
    monkeypatch.setenv("CONNECTIONS_KEY", KEY)
    monkeypatch.setenv("WEB_BASE_URL", url)
    if ok:
        assert Settings().web_base_url == url
    else:
        with pytest.raises(ValidationError):
            Settings()
