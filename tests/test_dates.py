from datetime import date, datetime
from zoneinfo import ZoneInfo

from hub.clock import FixedClock
from hub.entries import resolve_due
from hub.llm import Classification
from hub.service import Service
from tests.fakes import FakeLlm


async def test_today_is_moscow_date_after_utc_midnight(db, users, settings):
    # 7 ноября 01:30 по Москве = 6 ноября 22:30 UTC: «сегодня» должно быть 7-е
    clock = FixedClock(datetime(2026, 11, 7, 1, 30, tzinfo=ZoneInfo("Europe/Moscow")))
    llm = FakeLlm(
        classifications=[Classification("task", "купить билеты", date(2026, 11, 13), None, None)]
    )
    reply = await Service(db, llm, clock, settings).text(users.a.tg_id, "купить билеты до пятницы")
    assert llm.calls[0]["today"] == date(2026, 11, 7)
    row = await (await db.execute("SELECT due_date FROM entries")).fetchone()
    assert row["due_date"] == "2026-11-13"
    assert reply is not None


def test_resolve_due_wrong_year():
    assert resolve_due(date(2025, 12, 1), date(2026, 11, 2)) == date(2026, 12, 1)


def test_resolve_due_far_past_dropped():
    assert resolve_due(date(2025, 3, 1), date(2026, 11, 2)) is None


def test_resolve_due_none_and_valid_unchanged():
    assert resolve_due(None, date(2026, 11, 2)) is None
    assert resolve_due(date(2026, 11, 13), date(2026, 11, 2)) == date(2026, 11, 13)


def test_resolve_due_yesterday_kept():
    assert resolve_due(date(2026, 11, 1), date(2026, 11, 2)) == date(2026, 11, 1)


def test_resolve_due_leap_day_invalid_year_dropped():
    assert resolve_due(date(2024, 2, 29), date(2026, 1, 10)) is None
