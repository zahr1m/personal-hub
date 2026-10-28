-- Начальная схема personal-hub: даты — ISO YYYY-MM-DD, моменты — ISO с часовым поясом.

CREATE TABLE users (
    id         INTEGER PRIMARY KEY,
    tg_id      INTEGER UNIQUE NOT NULL,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    brief_time TEXT NOT NULL DEFAULT '09:00',
    created_at TEXT NOT NULL
);

CREATE TABLE invites (
    code       TEXT PRIMARY KEY,
    created_by INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    used_by    INTEGER REFERENCES users(id),
    used_at    TEXT
);

CREATE TABLE spaces (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE space_members (
    space_id INTEGER NOT NULL REFERENCES spaces(id),
    user_id  INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (space_id, user_id)
);

CREATE TABLE sections (
    id          INTEGER PRIMARY KEY,
    owner_id    INTEGER NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    archived_at TEXT,
    UNIQUE (owner_id, name)
);

CREATE TABLE entries (
    id         INTEGER PRIMARY KEY,
    owner_id   INTEGER NOT NULL REFERENCES users(id),
    kind       TEXT NOT NULL CHECK (kind IN ('task', 'project', 'think', 'note')),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    due_date   TEXT,
    contour    TEXT,
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    parent_id  INTEGER REFERENCES entries(id),
    section_id INTEGER REFERENCES sections(id),
    source     TEXT NOT NULL DEFAULT 'bot',
    created_at TEXT NOT NULL,
    done_at    TEXT
);

CREATE TABLE shares (
    object_type    TEXT NOT NULL CHECK (object_type IN ('entry', 'section')),
    object_id      INTEGER NOT NULL,
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user', 'space')),
    principal_id   INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (object_type, object_id, principal_type, principal_id)
);

CREATE TABLE llm_usage (
    user_id    INTEGER NOT NULL,
    day        TEXT NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE TABLE briefs (
    user_id  INTEGER NOT NULL,
    day      TEXT NOT NULL,
    status   TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'raw', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at  TEXT,
    error    TEXT,
    PRIMARY KEY (user_id, day)
);
