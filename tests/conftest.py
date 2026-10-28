from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet

from hub.clock import FixedClock
from hub.config import Settings
from hub.db import open_db
from hub.models import User
from hub.users import create_invite, ensure_admin, redeem_invite


@pytest.fixture
async def db(tmp_path):
    conn = await open_db(tmp_path / "hub.db")
    yield conn
    await conn.close()


@pytest.fixture
def clock():
    # понедельник
    return FixedClock(datetime(2026, 11, 2, 8, 0, tzinfo=ZoneInfo("Europe/Moscow")))


@pytest.fixture
def settings(tmp_path):
    return Settings(
        bot_token="test-token",
        anthropic_api_key="test-key",
        admin_tg_id=1,
        connections_key=Fernet.generate_key().decode(),
        db_path=tmp_path / "hub.db",
        backup_dir=tmp_path / "backups",
    )


@dataclass
class Users:
    a: User  # админ, в «Семье»
    b: User  # по приглашению, в «Семье»
    c: User  # вне «Семьи»


@pytest.fixture
async def users(db, clock):
    a = await ensure_admin(db, 1, "А", clock)
    b = await redeem_invite(db, await create_invite(db, a, clock), 2, "Б", clock)
    c = await redeem_invite(db, await create_invite(db, a, clock), 3, "В", clock)
    await db.execute("DELETE FROM space_members WHERE user_id = ?", (c.id,))
    await db.commit()
    return Users(a, b, c)
