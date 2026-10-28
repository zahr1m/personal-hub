"""Разбор ICS без сети и БД: этот модуль грузится в дочернем процессе, поэтому он лёгкий
(только stdlib, icalendar, recurring_ical_events)."""

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import icalendar
import recurring_ical_events

from hub.clock import MSK

MAX_OCCURRENCES = 2000
BAD_RRULE_FREQ = {"SECONDLY", "MINUTELY", "HOURLY"}
BAD_RRULE_BY = ("BYSECOND", "BYMINUTE")
MAX_VEVENTS = 5000
MAX_LIST_VALUES = 500
CHILD_DATA = 160 * 1024 * 1024  # RLIMIT_DATA ребёнка: держим далеко от лимита контейнера
NO_TITLE = "(без названия)"


class CalendarError(ValueError):
    """Ошибка с человеческим текстом, в котором нет адреса и содержимого ответа."""


@dataclass(frozen=True)
class Event:
    uid: str
    starts_at: datetime
    ends_at: datetime | None
    all_day: bool
    title: str
    location: str | None


def _to_msk(value: date | datetime) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        return (value.astimezone(MSK) if value.tzinfo else value.replace(tzinfo=MSK)), False
    return datetime.combine(value, time(), MSK), True


def _too_heavy(comp: icalendar.Component) -> bool:
    """RRULE чаще раза в день или с перебором минут/секунд: разворачивать нельзя."""
    rules = comp.get("RRULE")
    if rules is None:
        return False
    for rule in rules if isinstance(rules, list) else [rules]:
        if any(str(f).upper() in BAD_RRULE_FREQ for f in rule.get("FREQ", [])):
            return True
        if any(k in rule for k in BAD_RRULE_BY):
            return True
    return False


def _parse(raw: bytes, day_from: date, day_to: date) -> list[Event]:
    start = datetime.combine(day_from, time(), MSK)
    end = datetime.combine(day_to + timedelta(days=1), time(), MSK)
    cal = icalendar.Calendar.from_ical(raw)
    # без DTSTART библиотека падает; частые повторы взорвали бы память и процессор
    cal.subcomponents = [
        c for c in cal.subcomponents if c.name != "VEVENT" or ("DTSTART" in c and not _too_heavy(c))
    ]
    query = recurring_ical_events.of(cal)
    comps: list[icalendar.Component] = []
    for series in query.series:  # по одной серии: общий потолок проверяем на ходу
        comps += query._occurrences_to_components(series.between(start, end))
        if len(comps) > MAX_OCCURRENCES:
            raise CalendarError("в календаре слишком много повторов")
    events: list[Event] = []
    for comp in comps:
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        starts_at, all_day = _to_msk(comp["DTSTART"].dt)
        if "DTEND" in comp:
            ends_at = _to_msk(comp["DTEND"].dt)[0]
        elif "DURATION" in comp:
            ends_at = starts_at + comp["DURATION"].dt
        else:
            ends_at = None
        title = str(comp.get("SUMMARY", "")).strip() or NO_TITLE
        location = str(comp.get("LOCATION", "")).strip() or None
        uid = (
            str(comp.get("UID", "")).strip()
            or hashlib.sha256(f"{title}|{starts_at.isoformat()}".encode()).hexdigest()[:32]
        )
        events.append(Event(uid, starts_at, ends_at, all_day, title, location))
    events.sort(key=lambda e: (not e.all_day, e.starts_at))
    return events


def parse_events(raw: bytes, day_from: date, day_to: date) -> list[Event]:
    """События, пересекающие окно [day_from 00:00, day_to+1 00:00) по Москве, с повторами."""
    try:
        return _parse(raw, day_from, day_to)
    except CalendarError:
        raise
    except Exception:
        # тексты библиотек могут нести куски ответа — наружу не отдаём
        raise CalendarError("не удалось разобрать календарь") from None


def _precheck(raw: bytes) -> None:
    """Дёшево отсекаем явно тяжёлые файлы до запуска разбора (с разворотом складок и регистра)."""
    text = re.sub(rb"\r?\n[ \t]", b"", raw).lower()
    chunks = text.split(b"begin:vevent")
    if len(chunks) - 1 > MAX_VEVENTS:
        raise CalendarError("в календаре слишком много событий")
    for chunk in chunks[1:]:
        values = 0
        for line in chunk.splitlines():
            if line.startswith((b"rdate", b"exdate")):
                values += line.count(b",") + 1
        if values > MAX_LIST_VALUES:
            raise CalendarError("в календаре слишком много дат в одном событии")


def _child(pipe, raw: bytes, day_from: date, day_to: date) -> None:
    """Дочерний процесс: разбор и отправка простых кортежей (его можно убить по таймауту)."""
    try:
        try:
            import resource  # только Linux

            resource.setrlimit(resource.RLIMIT_DATA, (CHILD_DATA, CHILD_DATA))
        except (ImportError, ValueError, OSError):
            pass
        events = parse_events(raw, day_from, day_to)
        pipe.send(
            (
                "ok",
                [
                    (
                        e.uid,
                        e.starts_at.isoformat(),
                        e.ends_at.isoformat() if e.ends_at else None,
                        e.all_day,
                        e.title,
                        e.location,
                    )
                    for e in events
                ],
            )
        )
    except CalendarError as exc:
        pipe.send(("err", str(exc)))
    except BaseException:  # включая MemoryError
        pipe.send(("err", "не удалось разобрать календарь"))
    finally:
        pipe.close()
