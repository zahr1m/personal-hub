"""Лимиты разбора ICS: тяжёлые календари не должны ни зависать, ни есть память."""

import time
from datetime import date
from pathlib import Path

import icalendar
import pytest
import recurring_ical_events
from recurring_ical_events import CalendarQuery

from hub.calendar_ics import CalendarError, parse_events_safe

ICS = (Path(__file__).parent / "fixtures" / "calendar.ics").read_bytes()
HDR = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n"
FTR = "END:VCALENDAR\r\n"


def _ev(i: int, extra: str) -> str:
    return (
        f"BEGIN:VEVENT\r\nUID:{i}@t\r\nDTSTAMP:20261101T000000Z\r\n{extra}\r\n"
        "SUMMARY:x\r\nEND:VEVENT\r\n"
    )


def _cal(n: int, extra: str) -> bytes:
    return (HDR + "".join(_ev(i, extra) for i in range(n)) + FTR).encode()


def test_private_library_api_present():
    # пин recurring-ical-events==3.8.2: при обновлении этот тест должен упасть громко
    assert hasattr(recurring_ical_events.of(icalendar.Calendar()), "series")
    assert hasattr(CalendarQuery, "_occurrences_to_components")


async def test_normal_fixture_parses_in_child_process():
    events = await parse_events_safe(ICS, date(2026, 11, 3), date(2026, 11, 3))
    assert [e.title for e in events] == ["Разовая встреча"]


async def test_old_dtstart_daily_is_killed_by_timeout():
    raw = _cal(3, "DTSTART:00010101T000000Z\r\nRRULE:FREQ=DAILY")
    t0 = time.monotonic()
    with pytest.raises(CalendarError, match="прерван"):
        await parse_events_safe(raw, date(2026, 11, 1), date(2026, 11, 9), parse_timeout=2)
    assert time.monotonic() - t0 < 10


async def test_too_many_rdates_rejected_fast():
    vals = ",".join(f"202611{d:02d}T{h:02d}0000Z" for d in range(1, 10) for h in range(24))
    raw = _cal(1, f"DTSTART:20261103T000000Z\r\nRDATE:{vals},{vals},{vals}")
    t0 = time.monotonic()
    with pytest.raises(CalendarError, match="дат"):
        await parse_events_safe(raw, date(2026, 11, 1), date(2026, 11, 9))
    assert time.monotonic() - t0 < 1


async def test_too_many_exdates_in_folded_line_rejected():
    vals = ",".join(
        f"2026110{d}T{h:02d}{m:02d}00Z" for d in range(1, 10) for h in range(24) for m in range(3)
    )
    folded = "\r\n ".join(vals[i : i + 70] for i in range(0, len(vals), 70))
    raw = _cal(1, f"DTSTART:20261103T000000Z\r\nEXDATE:{folded}")
    with pytest.raises(CalendarError, match="дат"):
        await parse_events_safe(raw, date(2026, 11, 1), date(2026, 11, 9))


async def test_too_many_vevents_rejected():
    raw = _cal(5001, "DTSTART:20261103T100000Z")
    with pytest.raises(CalendarError, match="событий"):
        await parse_events_safe(raw, date(2026, 11, 1), date(2026, 11, 9))


async def test_child_error_is_reported_as_calendar_error():
    raw = _cal(300, "DTSTART:20261103T100000Z\r\nRRULE:FREQ=DAILY")
    with pytest.raises(CalendarError, match="повтор"):
        await parse_events_safe(raw, date(2026, 11, 1), date(2026, 11, 9))
