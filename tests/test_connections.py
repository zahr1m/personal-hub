import pytest
from cryptography.fernet import Fernet

from hub.connections import (
    add_connection,
    disable_connection,
    get_secret,
    list_connections,
)
from hub.crypto import SecretBox

URL = "https://calendar.example.com/private/abc123/basic.ics"


@pytest.fixture
def box():
    return SecretBox(Fernet.generate_key().decode())


async def test_get_secret_other_user_permission_error(db, box, users, clock):
    conn = await add_connection(
        db, box, users.a, "ics_calendar", "Работа", URL, "calendar.example.com", clock
    )
    assert await get_secret(db, box, users.a, conn.id) == URL
    with pytest.raises(PermissionError):
        await get_secret(db, box, users.b, conn.id)


async def test_secret_not_stored_plain(db, box, users, clock):
    conn = await add_connection(
        db, box, users.a, "ics_calendar", "Работа", URL, "calendar.example.com", clock
    )
    row = await (await db.execute("SELECT * FROM connections WHERE id = ?", (conn.id,))).fetchone()
    assert b"abc123" not in bytes(row["secret_enc"])
    others = [str(v) for k, v in dict(row).items() if k != "secret_enc"]
    assert "abc123" not in " ".join(others)
    assert conn.hint == "calendar.example.com"
    assert conn.status == "pending"


async def test_list_connections_only_own(db, box, users, clock):
    await add_connection(db, box, users.a, "ics_calendar", "А", URL, "h", clock)
    await add_connection(db, box, users.b, "ics_calendar", "Б", URL, "h", clock)
    assert [c.name for c in await list_connections(db, users.a)] == ["А"]


async def test_disable_connection_wipes_secret_keeps_row(db, box, users, clock):
    conn = await add_connection(db, box, users.a, "ics_calendar", "А", URL, "h", clock)
    with pytest.raises(PermissionError):
        await disable_connection(db, users.b, conn.id, clock)
    await disable_connection(db, users.a, conn.id, clock)
    (after,) = await list_connections(db, users.a)
    assert after.status == "disabled"
    assert await get_secret(db, box, users.a, conn.id) == ""
