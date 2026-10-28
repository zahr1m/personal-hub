-- Веб-панель: сессии, ссылки входа, подключения (iCal) и их события.

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT,
    via TEXT NOT NULL CHECK (via IN ('link', 'tma'))
);

CREATE TABLE login_links (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    used_at TEXT
);

CREATE TABLE connections (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL CHECK (kind IN ('ics_calendar')),
    name TEXT NOT NULL,
    secret_enc BLOB NOT NULL,
    hint TEXT NOT NULL,  -- хост ссылки для показа
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'ok', 'error', 'auth_expired', 'disabled')),
    last_sync_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE calendar_events (
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    uid TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    all_day INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    location TEXT,
    PRIMARY KEY (connection_id, uid, starts_at)
);
