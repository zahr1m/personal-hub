"""Дочерний процесс разбора ICS: лёгкий импорт, обходы предпроверки, сбой запуска."""

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from hub import calendar_ics
from hub.calendar_ics import CalendarError, parse_events_safe

ICS = (Path(__file__).parent / "fixtures" / "calendar.ics").read_bytes()
DAY = date(2026, 11, 3)


def test_ics_parse_import_is_slim():
    code = (
        "import sys, hub.ics_parse;"
        "bad = [m for m in ('httpx', 'aiosqlite', 'cryptography') if m in sys.modules];"
        "print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


async def test_tab_folded_exdates_over_limit_rejected():
    vals = ",".join(
        f"2026110{d}T{h:02d}{m:02d}00Z" for d in range(1, 10) for h in range(24) for m in range(3)
    )
    folded = "\r\n\t".join(vals[i : i + 70] for i in range(0, len(vals), 70))
    raw = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nDTSTART:20261103T000000Z\r\n"
        f"EXDATE:{folded}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()
    with pytest.raises(CalendarError, match="дат"):
        await parse_events_safe(raw, DAY, DAY)


async def test_lowercase_markers_do_not_bypass_precheck():
    raw = b"begin:vevent\r\nend:vevent\r\n" * 5001
    with pytest.raises(CalendarError, match="событий"):
        await parse_events_safe(raw, DAY, DAY)
    vals = ",".join(f"2026110{d}T{h:02d}0000Z" for d in range(1, 10) for h in range(24))
    low = f"begin:vevent\r\nexdate:{vals},{vals},{vals}\r\nend:vevent\r\n".encode()
    with pytest.raises(CalendarError, match="дат"):
        await parse_events_safe(low, DAY, DAY)


async def test_process_start_failure_becomes_calendar_error(monkeypatch):
    # локальная лямбда не пиклится -> proc.start() падает ещё в родителе
    monkeypatch.setattr(calendar_ics, "_child", lambda *a: None)
    with pytest.raises(CalendarError, match="разобрать"):
        await parse_events_safe(ICS, DAY, DAY)


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_DATA есть только на Linux")
async def test_fixture_parses_under_memory_limit():
    events = await parse_events_safe(ICS, DAY, DAY)
    assert [e.title for e in events] == ["Разовая встреча"]
