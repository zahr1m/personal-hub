import socket
from datetime import date
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from hub.calendar_ics import (
    CalendarError,
    UnsafeUrl,
    check_url,
    events_for_day,
    fetch_ics,
    parse_events,
    sync_connection,
)
from hub.connections import add_connection, disable_connection, list_connections
from hub.crypto import SecretBox

ICS = (Path(__file__).parent / "fixtures" / "calendar.ics").read_bytes()
URL = "https://calendar.example.com/private/SECRET/basic.ics"


def resolver_to(*ips):
    async def resolve(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    return resolve


PUBLIC = resolver_to("93.184.216.34")


def test_parse_simple_and_recurring_and_all_day():
    events = parse_events(ICS, date(2026, 10, 27), date(2026, 11, 9))
    titles = [e.title for e in events]
    assert titles.count("Еженедельная планёрка") == 2  # 28.10 и 04.11
    assert "(без названия)" in titles
    one = next(e for e in events if e.title == "Разовая встреча")
    assert one.starts_at.hour == 10
    assert one.location == "Офис"
    assert not one.all_day
    day = next(e for e in events if e.title == "День рождения")
    assert day.all_day
    assert day.starts_at.date() == date(2026, 11, 4)
    utc = next(e for e in events if e.title == "Ночной созвон")
    # 23:00 UTC -> 02:00 МСК следующего дня
    assert utc.starts_at.date() == date(2026, 11, 6)
    assert utc.starts_at.hour == 2
    assert utc.starts_at.utcoffset().total_seconds() == 3 * 3600


def test_parse_window_bounds():
    events = parse_events(ICS, date(2026, 11, 3), date(2026, 11, 3))
    assert [e.title for e in events] == ["Разовая встреча"]


def test_event_without_dtstart_skipped():
    events = parse_events(ICS, date(2026, 10, 1), date(2026, 12, 31))
    assert "Без даты" not in [e.title for e in events]


def test_naive_datetime_treated_as_moscow():
    raw = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:n@t\r\n"
        b"DTSTAMP:20261101T000000Z\r\nDTSTART:20261103T100000\r\nSUMMARY:Plain\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    (e,) = parse_events(raw, date(2026, 11, 3), date(2026, 11, 3))
    assert e.starts_at.hour == 10
    assert e.starts_at.utcoffset().total_seconds() == 3 * 3600


@pytest.mark.parametrize(
    "url",
    [
        "http://calendar.example.com/a.ics",
        "ftp://calendar.example.com/a.ics",
        "https://user:pw@calendar.example.com/a.ics",
        "https:///a.ics",
    ],
)
async def test_check_url_rejects_bad_scheme_and_shape(url):
    with pytest.raises(UnsafeUrl):
        await check_url(url, PUBLIC)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
    ],
)
async def test_check_url_rejects_private_loopback_linklocal(ip):
    with pytest.raises(UnsafeUrl):
        await check_url("https://x.example.com/a.ics", resolver_to(ip))


async def test_check_url_rejects_if_any_address_private():
    with pytest.raises(UnsafeUrl):
        await check_url("https://x.example.com/", resolver_to("93.184.216.34", "10.0.0.1"))


async def test_check_url_rejects_unresolvable():
    async def boom(host, port):
        raise socket.gaierror("nope")

    with pytest.raises(UnsafeUrl):
        await check_url("https://x.example.com/", boom)


async def test_check_url_accepts_public():
    await check_url("https://x.example.com/a.ics", PUBLIC)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def test_fetch_ok_and_relative_redirect():
    def handler(req):
        if req.url.path == "/a.ics":
            return httpx.Response(302, headers={"Location": "/b.ics"})
        return httpx.Response(200, content=ICS)

    async with client_for(handler) as c:
        assert await fetch_ics("https://x.example.com/a.ics", c, PUBLIC) == ICS


async def test_redirect_to_metadata_ip_rejected():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(302, headers={"Location": "https://metadata.internal/x"})

    async def resolver(host, port):
        ip = "169.254.169.254" if host == "metadata.internal" else "93.184.216.34"
        return await resolver_to(ip)(host, port)

    async with client_for(handler) as c:
        with pytest.raises(UnsafeUrl):
            await fetch_ics("https://x.example.com/a.ics", c, resolver)
    assert seen == ["https://x.example.com/a.ics"]  # на внутренний адрес запроса не было


async def test_redirect_to_http_rejected():
    def handler(req):
        return httpx.Response(301, headers={"Location": "http://x.example.com/a.ics"})

    async with client_for(handler) as c:
        with pytest.raises(UnsafeUrl):
            await fetch_ics("https://x.example.com/a.ics", c, PUBLIC)


async def test_too_many_redirects():
    def handler(req):
        return httpx.Response(302, headers={"Location": "/again"})

    async with client_for(handler) as c:
        with pytest.raises(ValueError, match="редирект"):
            await fetch_ics("https://x.example.com/a.ics", c, PUBLIC)


async def test_fetch_over_2mb_rejected():
    def handler(req):
        # без Content-Length: размер считается по приходящим байтам
        async def body():
            for _ in range(3):
                yield b"x" * (1024 * 1024)

        return httpx.Response(200, content=body())

    async with client_for(handler) as c:
        with pytest.raises(ValueError, match="2 МБ"):
            await fetch_ics("https://x.example.com/a.ics", c, PUBLIC)


async def test_fetch_non_2xx():
    async with client_for(lambda r: httpx.Response(404)) as c:
        with pytest.raises(ValueError, match="404"):
            await fetch_ics("https://x.example.com/a.ics", c, PUBLIC)


async def test_fetch_timeout():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    async with client_for(handler) as c:
        with pytest.raises(ValueError, match="20 с"):
            await fetch_ics("https://x.example.com/a.ics", c, PUBLIC)


@pytest.fixture
def box():
    return SecretBox(Fernet.generate_key().decode())


async def _conn(db, box, user, clock):
    return await add_connection(
        db, box, user, "ics_calendar", "Работа", URL, "calendar.example.com", clock
    )


async def test_sync_replaces_window_and_sets_ok(db, box, users, clock):
    conn = await _conn(db, box, users.a, clock)
    async with client_for(lambda r: httpx.Response(200, content=ICS)) as c:
        await sync_connection(db, box, conn, c, clock, PUBLIC)
        await sync_connection(db, box, conn, c, clock, PUBLIC)  # повтор не дублирует
    events = await events_for_day(db, users.a, date(2026, 11, 3))
    assert [e.title for e in events] == ["Разовая встреча"]
    (after,) = await list_connections(db, users.a)
    assert after.status == "ok"
    assert after.last_success_at is not None
    assert after.last_error is None


async def test_sync_error_keeps_old_events_and_sets_error(db, box, users, clock):
    conn = await _conn(db, box, users.a, clock)
    async with client_for(lambda r: httpx.Response(200, content=ICS)) as c:
        await sync_connection(db, box, conn, c, clock, PUBLIC)
    async with client_for(lambda r: httpx.Response(500)) as c:
        await sync_connection(db, box, conn, c, clock, PUBLIC)
    (after,) = await list_connections(db, users.a)
    assert after.status == "error"
    assert "500" in after.last_error
    assert "SECRET" not in after.last_error
    assert after.last_success_at is not None
    assert len(await events_for_day(db, users.a, date(2026, 11, 3))) == 1


async def test_sync_unsafe_url_error_has_no_secret(db, box, users, clock):
    conn = await _conn(db, box, users.a, clock)
    async with client_for(lambda r: httpx.Response(200, content=ICS)) as c:
        await sync_connection(db, box, conn, c, clock, resolver_to("10.0.0.1"))
    (after,) = await list_connections(db, users.a)
    assert after.status == "error"
    assert "SECRET" not in after.last_error


async def test_sync_garbage_ics_sets_error(db, box, users, clock):
    conn = await _conn(db, box, users.a, clock)
    async with client_for(lambda r: httpx.Response(200, content=b"<html>not ics")) as c:
        await sync_connection(db, box, conn, c, clock, PUBLIC)
    (after,) = await list_connections(db, users.a)
    assert after.status == "error"


async def test_events_for_day_only_own_connections(db, box, users, clock):
    ca = await _conn(db, box, users.a, clock)
    cb = await _conn(db, box, users.b, clock)
    async with client_for(lambda r: httpx.Response(200, content=ICS)) as c:
        await sync_connection(db, box, ca, c, clock, PUBLIC)
        await sync_connection(db, box, cb, c, clock, PUBLIC)
    assert len(await events_for_day(db, users.a, date(2026, 11, 3))) == 1
    assert await events_for_day(db, users.c, date(2026, 11, 3)) == []


async def test_events_for_day_skips_disabled_and_orders_all_day_first(db, box, users, clock):
    conn = await _conn(db, box, users.a, clock)
    await db.executemany(
        "INSERT INTO calendar_events(connection_id, uid, starts_at, ends_at, all_day, title)"
        " VALUES (?, ?, ?, NULL, ?, ?)",
        [
            (conn.id, "a", "2026-11-03T09:00:00+03:00", 0, "Поздно"),
            (conn.id, "b", "2026-11-03T08:00:00+03:00", 0, "Рано"),
            (conn.id, "c", "2026-11-03T00:00:00+03:00", 1, "Весь день"),
        ],
    )
    await db.commit()
    got = [e.title for e in await events_for_day(db, users.a, date(2026, 11, 3))]
    assert got == ["Весь день", "Рано", "Поздно"]
    await disable_connection(db, users.a, conn.id, clock)
    assert await events_for_day(db, users.a, date(2026, 11, 3)) == []


def _ics(*vevents: str) -> bytes:
    body = "".join(
        "BEGIN:VEVENT\r\nDTSTAMP:20261101T000000Z\r\n" + v + "\r\nEND:VEVENT\r\n" for v in vevents
    )
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n" + body + "END:VCALENDAR\r\n").encode()


async def test_secondly_rrule_dropped_fast():
    import time

    raw = _ics(
        "UID:s@t\r\nDTSTART:20261103T100000Z\r\nRRULE:FREQ=SECONDLY\r\nSUMMARY:Bomb",
        "UID:ok@t\r\nDTSTART:20261103T100000Z\r\nSUMMARY:Ok",
    )
    t0 = time.monotonic()
    events = parse_events(raw, date(2026, 11, 3), date(2026, 11, 3))
    assert time.monotonic() - t0 < 1
    assert [e.title for e in events] == ["Ok"]


async def test_too_many_occurrences_rejected():
    from hub.calendar_ics import CalendarError

    one = "UID:d{}@t\r\nDTSTART:20261103T100000Z\r\nRRULE:FREQ=DAILY\r\nSUMMARY:x"
    many = [one.format(i) for i in range(300)]
    with pytest.raises(CalendarError, match="повтор"):
        parse_events(_ics(*many), date(2026, 11, 1), date(2026, 11, 9))


def test_cancelled_skipped_and_duration_end():
    raw = _ics(
        "UID:c@t\r\nDTSTART:20261103T100000Z\r\nSTATUS:CANCELLED\r\nSUMMARY:Gone",
        "UID:d@t\r\nDTSTART:20261103T100000Z\r\nDURATION:PT2H\r\nSUMMARY:Dur",
    )
    (e,) = parse_events(raw, date(2026, 11, 3), date(2026, 11, 3))
    assert e.title == "Dur"
    assert e.ends_at.hour == 15


async def test_error_text_does_not_leak_response_content(db, box, users, clock):
    conn = await _conn(db, box, users.a, clock)
    body = _ics("UID:l@t\r\nDTSTART:/private/SECRET/basic.ics")
    async with client_for(lambda r: httpx.Response(200, content=body)) as c:
        await sync_connection(db, box, conn, c, clock, PUBLIC)
    (after,) = await list_connections(db, users.a)
    assert after.status == "error"
    assert "SECRET" not in after.last_error


async def test_disable_during_fetch_keeps_disabled(db, box, users, clock):
    conn = await _conn(db, box, users.a, clock)

    async def handler(req):
        await disable_connection(db, users.a, conn.id, clock)
        return httpx.Response(200, content=ICS)

    async with client_for(handler) as c:
        await sync_connection(db, box, conn, c, clock, PUBLIC)
    (after,) = await list_connections(db, users.a)
    assert after.status == "disabled"


async def test_sync_disabled_does_nothing(db, box, users, clock):
    conn = await _conn(db, box, users.a, clock)
    await disable_connection(db, users.a, conn.id, clock)
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, content=ICS)

    async with client_for(handler) as c:
        await sync_connection(db, box, conn, c, clock, PUBLIC)
    assert calls == []


async def test_compressed_response_rejected():
    def handler(req):
        assert req.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"x")

    async with client_for(handler) as c:
        with pytest.raises(ValueError, match="сжатом"):
            await fetch_ics("https://x.example.com/a.ics", c, PUBLIC)


async def test_fetch_ignores_client_follow_redirects_setting():
    hits = []

    def handler(req):
        hits.append(req.url.host)
        return httpx.Response(302, headers={"Location": "https://metadata.internal/x"})

    async def resolver(host, port):
        ip = "169.254.169.254" if host == "metadata.internal" else "93.184.216.34"
        return await resolver_to(ip)(host, port)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    async with client as c:
        with pytest.raises(UnsafeUrl):
            await fetch_ics("https://x.example.com/a.ics", c, resolver)
    assert hits == ["x.example.com"]


async def test_multi_day_events_visible_on_overlapping_days(db, box, users, clock):
    conn = await _conn(db, box, users.a, clock)
    raw = _ics(
        "UID:m@t\r\nDTSTART;VALUE=DATE:20261103\r\nDTEND;VALUE=DATE:20261105\r\nSUMMARY:Trip",
        "UID:n@t\r\nDTSTART;TZID=Europe/Moscow:20261104T230000\r\n"
        "DTEND;TZID=Europe/Moscow:20261105T010000\r\nSUMMARY:Night",
    )
    async with client_for(lambda r: httpx.Response(200, content=raw)) as c:
        await sync_connection(db, box, conn, c, clock, PUBLIC)
    titles = {}
    for d in (3, 4, 5):
        titles[d] = [e.title for e in await events_for_day(db, users.a, date(2026, 11, d))]
    assert titles[3] == ["Trip"]
    assert titles[4] == ["Trip", "Night"]
    assert titles[5] == ["Night"]


async def test_sync_replaces_all_events_of_connection(db, box, users, clock):
    conn = await _conn(db, box, users.a, clock)
    await db.execute(
        "INSERT INTO calendar_events(connection_id, uid, starts_at, ends_at, all_day, title)"
        " VALUES (?, 'old', '2020-01-01T10:00:00+03:00', NULL, 0, 'Old')",
        (conn.id,),
    )
    await db.commit()
    async with client_for(lambda r: httpx.Response(200, content=ICS)) as c:
        await sync_connection(db, box, conn, c, clock, PUBLIC)
    cur = await db.execute("SELECT COUNT(*) AS n FROM calendar_events WHERE uid='old'")
    n = await cur.fetchone()
    assert n["n"] == 0


async def test_disable_during_fetch_writes_no_events(db, box, users, clock):
    conn = await _conn(db, box, users.a, clock)
    await db.execute(
        "INSERT INTO calendar_events(connection_id, uid, starts_at, ends_at, all_day, title)"
        " VALUES (?, 'old', '2026-11-03T10:00:00+03:00', NULL, 0, 'Old')",
        (conn.id,),
    )
    await db.commit()

    async def handler(req):
        await disable_connection(db, users.a, conn.id, clock)
        return httpx.Response(200, content=ICS)

    async with client_for(handler) as c:
        await sync_connection(db, box, conn, c, clock, PUBLIC)
    cur = await db.execute("SELECT uid FROM calendar_events WHERE connection_id = ?", (conn.id,))
    assert [r["uid"] for r in await cur.fetchall()] == ["old"]


@pytest.mark.parametrize(
    "url",
    [
        "https://calendar.example.com/TOP\tSECRET/a.ics",
        "https://calendar.example.com/TOP\x00SECRET/a.ics",
        "https://xn--zz.com/SECRET/a.ics",
    ],
)
async def test_fetch_invalid_url_is_calendar_error_without_secret(url):
    def handler(req):
        return httpx.Response(200, content=b"x")

    async with client_for(handler) as c:
        with pytest.raises(CalendarError) as exc:
            await fetch_ics(url, c, PUBLIC)
    assert "SECRET" not in str(exc.value)


async def test_check_url_unicode_error_from_resolver_is_unsafe():
    async def boom(host, port):
        raise UnicodeError("label empty or too long")

    with pytest.raises(UnsafeUrl):
        await check_url("https://x.example.com/", boom)
