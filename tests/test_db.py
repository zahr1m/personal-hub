import sqlite3

import pytest

from hub.db import open_db


async def test_open_db_applies_migrations_once(tmp_path):
    db = await open_db(tmp_path / "t.db")
    await db.close()
    db = await open_db(tmp_path / "t.db")
    rows = await (await db.execute("SELECT name FROM schema_migrations")).fetchall()
    await db.close()
    assert [r["name"] for r in rows] == [
        "001_init.sql",
        "002_web.sql",
        "003_brief_intro.sql",
        "004_brief_manual.sql",
    ]


async def test_entries_kind_is_checked(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO entries(owner_id,kind,title,created_at) VALUES (1,'spam','x','t')"
        )


async def test_foreign_keys_and_wal_enabled(db):
    assert (await (await db.execute("PRAGMA foreign_keys")).fetchone())[0] == 1
    assert (await (await db.execute("PRAGMA journal_mode")).fetchone())[0] == "wal"


async def test_open_db_fails_without_migrations(tmp_path):
    empty = tmp_path / "no-migrations"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="no-migrations"):
        await open_db(tmp_path / "t.db", migrations_dir=empty)


async def test_migrations_002_applied_and_recorded(db):
    rows = await (await db.execute("SELECT name FROM schema_migrations ORDER BY name")).fetchall()
    assert [r["name"] for r in rows] == [
        "001_init.sql",
        "002_web.sql",
        "003_brief_intro.sql",
        "004_brief_manual.sql",
    ]
    cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r["name"] for r in await cur.fetchall()}
    assert {"sessions", "login_links", "connections", "calendar_events"} <= tables


async def test_failed_migration_rolls_back_and_is_not_recorded(tmp_path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "001_ok.sql").write_text("CREATE TABLE ok (id INTEGER);", encoding="utf-8")
    (mdir / "003_bad.sql").write_text(
        "CREATE TABLE half (id INTEGER);\nINSERT INTO no_such_table VALUES (1);\n",
        encoding="utf-8",
    )
    path = tmp_path / "t.db"
    with pytest.raises(sqlite3.OperationalError):
        await open_db(path, migrations_dir=mdir)
    conn = sqlite3.connect(path)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        recorded = [r[0] for r in conn.execute("SELECT name FROM schema_migrations")]
    finally:
        conn.close()
    assert "half" not in names
    assert recorded == ["001_ok.sql"]
