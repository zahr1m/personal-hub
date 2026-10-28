"""Время в одном месте: все даты и расписания — по Москве."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


class Clock:
    def now(self) -> datetime:
        return datetime.now(MSK)

    def today(self) -> date:
        return self.now().date()


class FixedClock(Clock):
    """Замороженные часы для тестов."""

    def __init__(self, dt: datetime) -> None:
        self._dt = dt

    def now(self) -> datetime:
        return self._dt
