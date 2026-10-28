import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hub.models import User
from hub.scheduler import (
    _brief_job,
    _watchdog_job,
    backup,
    build_scheduler,
    run_brief,
    schedule_user,
    watchdog,
)
from tests.fakes import FakeLlm

DAY = date(2026, 11, 2)


class Outbox:
    """Запоминает отправленное; fail=True — отправка падает."""

    def __init__(self, fail=False):
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def __call__(self, tg_id, text):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((tg_id, text))


async def row(db, user):
    r = await (
        await db.execute(
            "SELECT * FROM briefs WHERE user_id = ? AND day = ?", (user.id, DAY.isoformat())
        )
    ).fetchone()
    return r


async def test_brief_sent_once_per_day(db, users):
    llm, out = FakeLlm(intros=["Привет", "Привет ещё раз"]), Outbox()
    assert await run_brief(db, llm, out, users.a, DAY) == "sent"
    assert await run_brief(db, llm, out, users.a, DAY) == "sent"
    assert len(out.sent) == 1
    assert out.sent[0][0] == users.a.tg_id
    r = await row(db, users.a)
    assert r["status"] == "sent"
    assert r["sent_at"]
    assert len(llm.calls) == 1


async def test_raw_retries_then_sends_raw_on_4th(db, users):
    llm, out = FakeLlm(fail=True), Outbox()
    for attempt in (1, 2, 3):
        assert await run_brief(db, llm, out, users.a, DAY) == "pending"
        r = await row(db, users.a)
        assert r["status"] == "pending"
        assert r["attempts"] == attempt
        assert r["error"]
    assert out.sent == []
    assert await run_brief(db, llm, out, users.a, DAY, final_attempt=True) == "raw"
    assert len(out.sent) == 1
    r = await row(db, users.a)
    assert r["status"] == "raw"
    assert r["attempts"] == 4
    # после raw повторы молчат
    assert await run_brief(db, llm, out, users.a, DAY, final_attempt=True) == "raw"
    assert len(out.sent) == 1


async def test_send_failure_marks_failed(db, users):
    llm = FakeLlm(intros=["Привет"])
    assert await run_brief(db, llm, Outbox(fail=True), users.a, DAY) == "failed"
    r = await row(db, users.a)
    assert r["status"] == "failed"
    assert r["attempts"] == 1
    assert "telegram down" in r["error"]


async def test_force_brief_ignores_sent_status(db, users):
    llm, out = FakeLlm(intros=["Привет"]), Outbox()
    await run_brief(db, llm, out, users.a, DAY)
    llm2 = FakeLlm(fail=True)  # сырой брифинг уходит сразу
    assert await run_brief(db, llm2, out, users.a, DAY, force=True) == "raw"
    assert len(out.sent) == 2
    assert (await row(db, users.a))["status"] == "sent"  # briefs не тронут


async def test_watchdog_reports_missing_brief(db, users):
    out = Outbox()
    await run_brief(db, FakeLlm(intros=["x"]), Outbox(fail=True), users.b, DAY)
    await watchdog(db, out, users.a, DAY, [users.a, users.b])
    texts = [t for _, t in out.sent]
    assert all(tg == users.a.tg_id for tg, _ in out.sent)
    assert texts == [
        "брифинг для А не ушёл: нет данных",
        "брифинг для Б не ушёл: telegram down",
    ]


async def test_watchdog_quiet_when_sent(db, users):
    out = Outbox()
    await run_brief(db, FakeLlm(intros=["x"]), Outbox(), users.a, DAY)
    await run_brief(db, FakeLlm(fail=True), Outbox(), users.b, DAY, final_attempt=True)
    await watchdog(db, out, users.a, DAY, [users.a, users.b])
    assert out.sent == []


def test_backup_keeps_last_7(tmp_path):
    src = tmp_path / "hub.db"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()
    dest = tmp_path / "backups"
    dest.mkdir()
    for d in range(1, 10):
        (dest / f"hub-2026-10-{d:02d}.db").write_bytes(b"old")
    (dest / "notes.txt").write_text("keep")
    backup(src, dest, date(2026, 11, 2))
    names = sorted(p.name for p in dest.glob("hub-*.db"))
    assert len(names) == 7
    assert names[-1] == "hub-2026-11-02.db"
    assert names[0] == "hub-2026-10-04.db"
    assert (dest / "notes.txt").exists()
    copy = sqlite3.connect(dest / "hub-2026-11-02.db")
    assert copy.execute("SELECT x FROM t").fetchone() == (42,)
    copy.close()


def test_build_scheduler_jobs(db, settings):
    u = User(1, 10, "А", "admin", "09:00")
    v = User(2, 20, "Б", "user", "23:50")
    sched = build_scheduler(db, FakeLlm(), Outbox(), settings, u, [u, v])
    jobs = {j.id: j for j in sched.get_jobs()}
    assert str(sched.timezone) == "Europe/Moscow"

    def hm(job):
        f = {x.name: str(x) for x in job.trigger.fields}
        return f["hour"], f["minute"]

    assert [hm(jobs[f"brief:1:{n}"]) for n in range(4)] == [
        ("9", "0"),
        ("9", "15"),
        ("9", "30"),
        ("9", "45"),
    ]
    assert hm(jobs["watchdog:1"]) == ("9", "16")
    assert hm(jobs["backup"]) == ("3", "30")
    assert hm(jobs["watchdog:2"]) == ("0", "6")  # переход через полночь
    assert all(j.misfire_grace_time == 600 for j in jobs.values())
    assert all(j.coalesce for j in jobs.values())


MSK = ZoneInfo("Europe/Moscow")


async def test_retry_after_midnight_belongs_to_base_day(db, users):
    out = Outbox()
    fire = lambda: datetime(2026, 11, 3, 0, 5, tzinfo=MSK)  # noqa: E731 — повтор +15 от 23:50
    await _brief_job(db, FakeLlm(intros=["x"]), out, users.a.id, timedelta(minutes=15), False, fire)
    assert await row(db, users.a) is not None  # день DAY (02.11), а не 03.11
    assert len(out.sent) == 1


async def test_watchdog_after_midnight_checks_base_day(db, users):
    out = Outbox()
    fire = lambda: datetime(2026, 11, 3, 0, 6, tzinfo=MSK)  # noqa: E731
    await _watchdog_job(db, out, users.a, users.b.id, timedelta(minutes=16), fire)
    assert len(out.sent) == 1  # у Б за 02.11 брифинга нет
    await run_brief(db, FakeLlm(intros=["x"]), Outbox(), users.b, DAY)
    await _watchdog_job(db, out, users.a, users.b.id, timedelta(minutes=16), fire)
    assert len(out.sent) == 1


async def test_job_skips_deleted_user(db, users):
    out = Outbox()
    fire = lambda: datetime(2026, 11, 2, 9, 0, tzinfo=MSK)  # noqa: E731
    await _brief_job(db, FakeLlm(), out, 999, timedelta(0), False, fire)
    assert out.sent == []


def test_schedule_user_adds_jobs_without_duplicates(db, settings):
    admin = User(1, 10, "А", "admin", "09:00")
    sched = build_scheduler(db, FakeLlm(), Outbox(), settings, admin, [admin])
    new = User(5, 50, "Н", "user", "10:00")
    schedule_user(sched, db, FakeLlm(), Outbox(), new, admin)
    schedule_user(sched, db, FakeLlm(), Outbox(), new, admin)
    ids = sorted(j.id for j in sched.get_jobs() if j.id.endswith(":5") or ":5:" in j.id)
    assert ids == ["brief:5:0", "brief:5:1", "brief:5:2", "brief:5:3", "watchdog:5"]
    assert len(sched.get_jobs()) == 5 + 5 + 1


def test_backup_missing_source_raises_and_keeps_backups(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "hub-2026-10-01.db").write_bytes(b"old")
    with pytest.raises(sqlite3.OperationalError):
        backup(tmp_path / "nope.db", dest, date(2026, 11, 2))
    assert [p.name for p in dest.iterdir()] == ["hub-2026-10-01.db"]


async def test_schedule_user_on_started_scheduler(db, users, settings, clock):
    scheduler = build_scheduler(db, FakeLlm(), Outbox(), settings, users.a, [users.a])
    scheduler.start(paused=True)
    try:
        schedule_user(scheduler, db, FakeLlm(), Outbox(), users.b, users.a)
        schedule_user(scheduler, db, FakeLlm(), Outbox(), users.b, users.a)
        ids = [j.id for j in scheduler.get_jobs() if j.id.split(":")[1:2] == [str(users.b.id)]]
        assert len(ids) == 5
    finally:
        scheduler.shutdown(wait=False)


class SentDuringBuild(FakeLlm):
    """Пока строится брифинг, параллельный запуск успевает записать sent."""

    def __init__(self, db, user, **kw):
        super().__init__(**kw)
        self._db, self._user = db, user

    async def brief_intro(self, user_id, facts_text):
        await self._db.execute(
            "INSERT INTO briefs(user_id, day, status, attempts) VALUES (?, ?, 'sent', 1)",
            (self._user.id, DAY.isoformat()),
        )
        await self._db.commit()
        return await super().brief_intro(user_id, facts_text)


async def test_brief_not_resent_if_sent_during_build(db, users):
    out = Outbox()
    llm = SentDuringBuild(db, users.a, intros=["Привет"])
    assert await run_brief(db, llm, out, users.a, DAY) == "sent"
    assert out.sent == []


class Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_calendar_sync_first_run_is_immediate(clock):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from hub import scheduler as sch

    sched = AsyncIOScheduler(timezone=clock.now().tzinfo)
    added = []
    real_add = sched.add_job
    sched.add_job = lambda *a, **kw: (added.append(kw), real_add(*a, **kw))[1]
    sch.schedule_calendar_sync(sched, None, None, Client, clock)
    assert added[0]["next_run_time"] == clock.now()


async def test_calendar_sync_job_registered_and_survives_error(db, users, clock, monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from cryptography.fernet import Fernet

    from hub import scheduler as sch
    from hub.connections import add_connection, disable_connection
    from hub.crypto import SecretBox

    box = SecretBox(Fernet.generate_key().decode())
    sched = AsyncIOScheduler(timezone=clock.now().tzinfo)
    sch.schedule_calendar_sync(sched, db, box, Client, clock)
    job = sched.get_job("calendar_sync")
    assert job.trigger.interval == timedelta(minutes=15)
    assert job.coalesce is True
    assert job.misfire_grace_time == 600

    first = await add_connection(db, box, users.a, "ics_calendar", "1", "https://a", "a", clock)
    off = await add_connection(db, box, users.a, "ics_calendar", "off", "https://o", "o", clock)
    await disable_connection(db, users.a, off.id, clock)
    other = await add_connection(db, box, users.b, "ics_calendar", "2", "https://b", "b", clock)
    seen = []

    async def fake_sync(db_, box_, conn, client, clock_, *args):
        seen.append(conn.id)
        if conn.id == first.id:
            raise RuntimeError("boom")

    monkeypatch.setattr(sch, "sync_connection", fake_sync)
    await job.func(*job.args)  # ошибка первого не мешает второму и не пробрасывается
    assert seen == [first.id, other.id]


async def test_run_brief_stores_intro_only_when_sent(db, users):
    assert (
        await run_brief(db, FakeLlm(intros=["Привет, день лёгкий"]), Outbox(), users.a, DAY)
        == "sent"
    )
    assert (await row(db, users.a))["intro"] == "Привет, день лёгкий"
    llm = FakeLlm(fail=True)
    assert await run_brief(db, llm, Outbox(), users.b, DAY, final_attempt=True) == "raw"
    assert (await row(db, users.b))["intro"] is None


async def test_force_brief_does_not_store_intro(db, users):
    await run_brief(db, FakeLlm(intros=["x"]), Outbox(), users.a, DAY, force=True)
    assert await row(db, users.a) is None
