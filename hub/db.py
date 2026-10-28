"""Подключение к SQLite и накат миграций."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

# migrations/ лежит в корне репозитория рядом с пакетом hub (в Docker — /app/migrations)
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


async def open_db(path: Path, migrations_dir: Path = MIGRATIONS_DIR) -> aiosqlite.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA journal_mode = WAL")
    try:
        await _migrate(db, migrations_dir)
    except BaseException:
        await db.close()  # иначе поток aiosqlite не даст процессу завершиться
        raise
    return db


def _sql_files(migrations_dir: Path) -> list[Path]:
    return sorted(migrations_dir.glob("*.sql"))


def _statements(sql: str) -> list[str]:
    """Режет файл миграции на statements (триггеры с `;` внутри не ломаются)."""
    out: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


async def _migrate(db: aiosqlite.Connection, migrations_dir: Path) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)"
    )
    await db.commit()
    rows = await (await db.execute("SELECT name FROM schema_migrations")).fetchall()
    applied = {r["name"] for r in rows}
    files = _sql_files(migrations_dir)
    if not files:
        raise RuntimeError(f"нет миграций в {migrations_dir}")
    for file in files:
        if file.name in applied:
            continue
        # миграция и её запись — одна транзакция; executescript не годится (делает commit)
        await db.execute("BEGIN")
        try:
            for stmt in _statements(file.read_text(encoding="utf-8")):
                await db.execute(stmt)
            await db.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (file.name, datetime.now(UTC).isoformat()),
            )
            await db.execute("COMMIT")
        except BaseException:
            await db.execute("ROLLBACK")
            raise
