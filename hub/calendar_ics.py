"""Календари по секретной ссылке iCal: безопасная загрузка, разбор, синхронизация.

Ссылка — секрет: её нельзя писать в логи, ошибки и ответы.
"""

import asyncio
import ipaddress
import logging
import multiprocessing
import socket
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from urllib.parse import urljoin, urlsplit

import aiosqlite
import httpx

from hub.clock import MSK, Clock
from hub.connections import Connection, set_status
from hub.crypto import SecretBox
from hub.ics_parse import CalendarError, Event, _child, _precheck, parse_events  # noqa: F401
from hub.models import User

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT_S = 20
REDIRECTS = {301, 302, 303, 307, 308}
PARSE_TIMEOUT_S = 8

log = logging.getLogger(__name__)
_parse_lock = asyncio.Lock()  # один разбор за раз: два плохих календаря не удвоят память

Resolver = Callable[[str, int], Awaitable[list]]


class UnsafeUrl(CalendarError):
    pass


async def _getaddrinfo(host: str, port: int) -> list:
    return await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def check_url(url: str, resolver: Resolver | None = None) -> None:
    """Только https, без логина в ссылке, и ни один адрес хоста не частный/служебный."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise UnsafeUrl("некорректная ссылка") from None
    if parts.scheme != "https":
        raise UnsafeUrl("ссылка должна начинаться с https://")
    if not host:
        raise UnsafeUrl("в ссылке нет хоста")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrl("логин и пароль в ссылке не поддерживаются")
    try:
        infos = await (resolver or _getaddrinfo)(host, port or 443)
    except (OSError, UnicodeError):  # кривой IDNA-хост бросает UnicodeError
        raise UnsafeUrl("хост ссылки не найден") from None
    if not infos:
        raise UnsafeUrl("хост ссылки не найден")
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            raise UnsafeUrl("хост ссылки не найден") from None
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        # is_global закрывает и частные, и loopback, и link-local, и CGNAT, и зарезервированные
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise UnsafeUrl("ссылка ведёт на внутренний адрес")


async def _get(url: str, client: httpx.AsyncClient) -> tuple[httpx.Response, bytes]:
    """Один запрос без редиректов; тело читается потоком и обрывается на 2 МБ."""
    async with client.stream(
        "GET",
        url,
        timeout=TIMEOUT_S,
        follow_redirects=False,
        headers={"Accept-Encoding": "identity"},  # размер считаем по байтам без распаковки
    ) as resp:
        if resp.status_code in REDIRECTS:
            return resp, b""
        if not 200 <= resp.status_code < 300:
            raise CalendarError(f"календарь ответил ошибкой {resp.status_code}")
        if resp.headers.get("content-encoding", "identity").lower() != "identity":
            raise CalendarError("календарь отдан в сжатом виде")
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BYTES:
            raise CalendarError("файл календаря больше 2 МБ")
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise CalendarError("файл календаря больше 2 МБ")
        return resp, bytes(body)


async def _fetch(url: str, client: httpx.AsyncClient, resolver: Resolver | None) -> bytes:
    for _ in range(MAX_REDIRECTS + 1):
        await check_url(url, resolver)
        resp, body = await _get(url, client)
        if resp.status_code not in REDIRECTS:
            return body
        location = resp.headers.get("location")
        if not location:
            raise CalendarError("календарь перенаправил без адреса")
        url = urljoin(url, location)
    raise CalendarError("слишком много редиректов")


async def fetch_ics(url: str, client: httpx.AsyncClient, resolver: Resolver | None = None) -> bytes:
    """Редиректы ведём сами (follow_redirects=False задаётся на каждом запросе)."""
    try:
        async with asyncio.timeout(TIMEOUT_S):
            return await _fetch(url, client, resolver)
    except (TimeoutError, httpx.TimeoutException):
        raise CalendarError("календарь не ответил за 20 с") from None
    except (httpx.InvalidURL, UnicodeError):  # IDNA-хост ломает httpx при сборке запроса
        raise CalendarError("некорректная ссылка") from None
    except httpx.HTTPError:
        # текст httpx-ошибок может содержать адрес — наружу не отдаём
        raise CalendarError("календарь недоступен") from None


async def parse_events_safe(
    raw: bytes, day_from: date, day_to: date, parse_timeout: float = PARSE_TIMEOUT_S
) -> list[Event]:
    """parse_events в отдельном процессе с жёстким таймаутом: зависший разбор убивается."""
    _precheck(raw)
    async with _parse_lock:
        ctx = multiprocessing.get_context("spawn")
        recv, send = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=_child, args=(send, raw, day_from, day_to), daemon=True)
        started = False
        try:
            try:
                await asyncio.to_thread(proc.start)
            except Exception:
                raise CalendarError("не удалось разобрать календарь") from None
            started = True
            send.close()
            if not await asyncio.to_thread(recv.poll, parse_timeout):
                raise CalendarError("календарь слишком сложный — разбор прерван")
            try:
                kind, payload = await asyncio.to_thread(recv.recv)
            except EOFError:
                raise CalendarError("не удалось разобрать календарь") from None
        finally:
            if started:
                if proc.is_alive():
                    proc.terminate()
                    await asyncio.to_thread(proc.join, 2)
                    if proc.is_alive():
                        proc.kill()
                await asyncio.to_thread(proc.join, 2)
            recv.close()
            send.close()
    if kind != "ok":
        raise CalendarError(payload)
    return [
        Event(
            uid,
            datetime.fromisoformat(st),
            datetime.fromisoformat(en) if en else None,
            all_day,
            title,
            location,
        )
        for uid, st, en, all_day, title, location in payload
    ]


async def sync_connection(
    db: aiosqlite.Connection,
    box: SecretBox,
    conn: Connection,
    client: httpx.AsyncClient,
    clock: Clock,
    resolver: Resolver | None = None,
    parse_timeout: float = PARSE_TIMEOUT_S,
) -> None:
    """Любая ошибка → статус error с короткой причиной; старые события остаются."""
    today = clock.today()
    day_from, day_to = today - timedelta(days=1), today + timedelta(days=7)
    try:
        row = await (
            await db.execute("SELECT secret_enc, status FROM connections WHERE id = ?", (conn.id,))
        ).fetchone()
        blob = bytes(row["secret_enc"]) if row else b""
        if row is None or row["status"] == "disabled" or not blob:
            return
        url = box.open(blob)
        raw = await fetch_ics(url, client, resolver)
        events = await parse_events_safe(raw, day_from, day_to, parse_timeout)
        try:
            # окно скользит, а события могут перекрывать его края: заменяем все события
            # отключённым за время загрузки подключениям ничего не пишем
            await db.execute(
                "DELETE FROM calendar_events WHERE connection_id = ?"
                " AND EXISTS (SELECT 1 FROM connections WHERE id = ? AND status != 'disabled')",
                (conn.id, conn.id),
            )
            await db.executemany(
                "INSERT OR REPLACE INTO calendar_events"
                "(connection_id, uid, starts_at, ends_at, all_day, title, location)"
                " SELECT ?, ?, ?, ?, ?, ?, ?"
                " WHERE EXISTS (SELECT 1 FROM connections WHERE id = ? AND status != 'disabled')",
                [
                    (
                        conn.id,
                        e.uid,
                        e.starts_at.isoformat(),
                        e.ends_at.isoformat() if e.ends_at else None,
                        int(e.all_day),
                        e.title,
                        e.location,
                        conn.id,
                    )
                    for e in events
                ],
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    except Exception as exc:
        log.warning("синхронизация календаря %s: %s", conn.id, type(exc).__name__)
        message = str(exc) if isinstance(exc, CalendarError) else "ошибка загрузки календаря"
        await set_status(db, conn.id, "error", message, clock, success=False)
        return
    await set_status(db, conn.id, "ok", None, clock, success=True)


async def events_for_day(db: aiosqlite.Connection, user: User, day: date) -> list[Event]:
    """События из своих неотключённых подключений: сначала на весь день, потом по времени."""
    lo = datetime.combine(day, time(), MSK).isoformat()
    hi = datetime.combine(day + timedelta(days=1), time(), MSK).isoformat()
    cur = await db.execute(
        "SELECT e.uid, e.starts_at, e.ends_at, e.all_day, e.title, e.location"
        " FROM calendar_events e JOIN connections c ON c.id = e.connection_id"
        " WHERE c.owner_id = ? AND c.status != 'disabled'"
        " AND e.starts_at < ? AND (e.starts_at >= ? OR e.ends_at > ?)"
        " ORDER BY e.all_day DESC, e.starts_at",
        (user.id, hi, lo, lo),
    )
    return [
        Event(
            r["uid"],
            datetime.fromisoformat(r["starts_at"]),
            datetime.fromisoformat(r["ends_at"]) if r["ends_at"] else None,
            bool(r["all_day"]),
            r["title"],
            r["location"],
        )
        for r in await cur.fetchall()
    ]
