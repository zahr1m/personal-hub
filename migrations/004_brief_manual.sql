-- вступление, собранное кнопкой на главной: отдельно от briefs, чтобы не влиять на утреннюю отправку
CREATE TABLE brief_manual (
    user_id    INTEGER NOT NULL REFERENCES users(id),
    day        TEXT NOT NULL,
    intro      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, day)
);
