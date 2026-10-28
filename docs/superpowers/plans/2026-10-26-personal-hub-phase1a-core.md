# Personal Hub — фаза 1a «Ядро, бот, брифинг» Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Работающий на VPS Telegram-бот для нескольких пользователей: принимает текст, раскладывает его на задачу, проект, «подумать» или заметку, раскладывает по разделам, даёт делиться записями и в 9:00 по местному времени присылает каждому брифинг по задачам.

**Architecture:** Один Python-процесс (`python -m hub`): aiogram long polling + APScheduler в одном event loop, SQLite через `aiosqlite`. Бот — тонкая обёртка над слоем `hub/service.py`, который возвращает готовые ответы; вся логика и проверка доступа живут ниже и тестируются без Telegram. LLM — только через `hub/llm.py` (Anthropic), с маскированием и дневным бюджетом на пользователя.

**Tech Stack:** Python 3.12, uv, aiogram 3, aiosqlite, APScheduler 3, anthropic SDK, pydantic-settings, pytest + pytest-asyncio, ruff, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-26-personal-hub-design.md`. При расхождении разделов прав §20 (так сказано в шапке спеки).

**Как фаза 1 из спеки нарезана на планы (выбор строителя, по правилу «один план — одна работающая подсистема»):**

| План | Что | Разделы спеки |
|---|---|---|
| **1a (этот)** | ядро, пользователи, доступ, записи, разделы, бот, брифинг по задачам, бюджет, деплой | §5, §9, §10, §11, §13, §14, §20.1, §20.4, §20.8 (без Notion) |
| 1b | реестр документов, облачное хранилище (WebDAV), карточки, сроки, `/ask`, бэкап в облако, `/think` | §6, §14, §20.4 |
| 1c | веб: вход по ссылке `/web`, «Настройки», мастер подключения облака и календарей, календарь в брифинге | §8, §12, §20.1, §20.2, §20.12 |
| 1d | семейный чат, голос через Gemini, файлы из бота в облако | §20.3, §20.7 |

Почта (§7) — фаза 2, Notion (§20.9) — фаза 2, остальной дашборд — фаза 3, MCP (§20.11) — фаза 5.

## Global Constraints

- Python `>=3.12,<3.13`; зависимости через `uv`, `uv.lock` в репозитории (как в типовом Python-проекте).
- Один контейнер, лимит памяти **300 МБ** (`mem_limit: 300m`) — §4, §14.
- Часовой пояс `Europe/Moscow` для всех дат и расписаний; в базе даты — ISO-строки `YYYY-MM-DD`, моменты — ISO с часовым поясом.
- Модели: `claude-haiku-5-5` — разбор записей; `claude-sonnet-5-5` — декомпозиция проекта и вступление брифинга (§13, §20.4).
- В каждый запрос к Anthropic `metadata.user_id = "personal-hub:<user_id>"` — метка, отделяющая расход этого проекта от других потребителей того же ключа (§13; поле `user_id` — единственное, которое принимает API).
- Дневной лимит на пользователя: **2 000 000 входящих / 200 000 исходящих токенов** (§13, §20.1).
- Перед отправкой в модель номера карт и паспортов маскируются (§13).
- Бот работает **long polling**, отвечает только зарегистрированным пользователям, остальным молчит (§11, §20.1).
- Ничего не удаляется безвозвратно: задачи закрываются статусом, разделы архивируются (§2).
- Сообщение Telegram ≤ **4096** символов (§10).
- Секреты только в `/opt/personal-hub/.env` с правами 600; в репозитории `.env.example` (§14).
- Ключ Anthropic на старте общий с другим проектом (техдолг, §16); бота создаёт владелец у @BotFather.

## Review Focus

1. **Незнакомый человек пишет боту** — ни ответа, ни записи в базе. Тест в Task 6 (`test_stranger_is_ignored_and_nothing_saved`).
2. **Приглашение использовано повторно или просрочено** (ссылку переслали) — второй человек не регистрируется и получает молчание, первый остаётся. Тесты в Task 2 (`test_invite_once`, `test_invite_expires_after_24h`) и Task 6 (`test_stranger_with_used_code_is_ignored`).
3. **«До пятницы» в 01:30 по местному времени, когда в UTC ещё вчера** — срок считается от локальной даты (Europe/Moscow в примерах), а не от UTC. Тест в Task 7.
4. **Модель вернула мусор или упала** — текст пользователя не теряется: запись сохраняется заметкой, пользователь видит «не разобрал, сохранил как заметку». Тест в Task 6 (`test_llm_failure_saves_note`).
5. **Двойное нажатие «✔ сделано»** или нажатие на кнопку чужой нерасшаренной записи — повтор ничего не ломает, чужая запись отвечает «нет доступа». Тесты в Task 4 (`test_complete_twice_keeps_done_at`, `test_complete_hidden_task_denied`) и Task 8 (`test_double_done_is_idempotent`).

---

## File Structure

```
personal-hub/
  pyproject.toml, uv.lock, .python-version, .env.example, Dockerfile, compose.yml
  migrations/001_init.sql          схема фазы 1a
  hub/
    __main__.py                    запуск: бот + планировщик
    config.py                      Settings из окружения
    clock.py                       Clock: текущее время в Europe/Moscow (подменяется в тестах)
    db.py                          открытие базы, прогон миграций
    models.py                      dataclass'ы записей и enum'ы
    access.py                      слой доступа: кто что видит, шаринг
    users.py                       пользователи, приглашения, пространства
    entries.py                     записи: создание, задачи, статусы, подзадачи
    sections.py                    разделы
    pii.py                         маскирование номеров
    budget.py                      дневной бюджет токенов
    llm.py                         обёртка Anthropic: разбор, декомпозиция, вступление брифинга
    brief.py                       сбор фактов брифинга и его текст
    service.py                     сценарии бота: принимает команду, возвращает Reply
    bot.py                         aiogram: роутер, кнопки, перевод Reply в сообщения
    scheduler.py                   брифинги по расписанию, повторы, сторож 9:15, бэкап
  tests/
    conftest.py                    фикстуры: db, clock, fake_llm, users
    fakes.py                       FakeLlm
    test_*.py
  ops/DEPLOY.md                    как ставить на сервер
```

Граница, которую держим: **только `access.py` знает правила видимости**. `entries.py`, `sections.py`, `brief.py` получают из него SQL-условие и параметры, своих правил не пишут.

---

### Task 1: Каркас проекта, конфиг, база и миграции

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.env.example`, `hub/__init__.py`, `hub/config.py`, `hub/clock.py`, `hub/db.py`, `migrations/001_init.sql`, `tests/conftest.py`
- Test: `tests/test_db.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `class Settings(BaseSettings)`: `bot_token: str`, `anthropic_api_key: str`, `admin_tg_id: int`, `db_path: Path = Path("/data/hub.db")`, `backup_dir: Path = Path("/data/backups")`, `tz: str = "Europe/Moscow"`, `brief_time: str = "09:00"`, `daily_tokens_in: int = 2_000_000`, `daily_tokens_out: int = 200_000`, `model_fast: str = "claude-haiku-5-5"`, `model_smart: str = "claude-sonnet-5-5"`.
  - `class Clock`: `now() -> datetime` (aware, Europe/Moscow), `today() -> date`. `FixedClock(dt)` для тестов.
  - `async def open_db(path: Path) -> aiosqlite.Connection` — включает `foreign_keys`, WAL, `row_factory = aiosqlite.Row`, прогоняет непрошедшие файлы `migrations/*.sql` по порядку, учитывая их в таблице `schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT)`.
  - Схема `001_init.sql`:
    - `users(id INTEGER PK, tg_id INTEGER UNIQUE NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','user')), brief_time TEXT NOT NULL DEFAULT '09:00', created_at TEXT NOT NULL)`
    - `invites(code TEXT PK, created_by INTEGER NOT NULL REFERENCES users, expires_at TEXT NOT NULL, used_by INTEGER REFERENCES users, used_at TEXT)`
    - `spaces(id INTEGER PK, name TEXT NOT NULL UNIQUE)`; `space_members(space_id, user_id, PRIMARY KEY(space_id,user_id))`
    - `sections(id INTEGER PK, owner_id INTEGER NOT NULL REFERENCES users, name TEXT NOT NULL, archived_at TEXT, UNIQUE(owner_id, name))`
    - `entries(id INTEGER PK, owner_id INTEGER NOT NULL REFERENCES users, kind TEXT NOT NULL CHECK(kind IN ('task','project','think','note')), title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', due_date TEXT, contour TEXT, status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done','dropped')), parent_id INTEGER REFERENCES entries, section_id INTEGER REFERENCES sections, source TEXT NOT NULL DEFAULT 'bot', created_at TEXT NOT NULL, done_at TEXT)`
    - `shares(object_type TEXT NOT NULL CHECK(object_type IN ('entry','section')), object_id INTEGER NOT NULL, principal_type TEXT NOT NULL CHECK(principal_type IN ('user','space')), principal_id INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(object_type, object_id, principal_type, principal_id))`
    - `llm_usage(user_id INTEGER NOT NULL, day TEXT NOT NULL, tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, day))`
    - `briefs(user_id INTEGER NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','sent','raw','failed')), attempts INTEGER NOT NULL DEFAULT 0, sent_at TEXT, error TEXT, PRIMARY KEY(user_id, day))`
- Фикстуры `conftest.py`: `db` (временный файл в `tmp_path`), `clock` (`FixedClock(2026-11-02 08:00 Europe/Moscow)`, понедельник), `settings` (`Settings` с фиктивными токенами). Фикстуру `users` (админ `a`, участники «Семьи» `b`, посторонний `c` вне «Семьи») добавляет Task 3.

- [ ] **Step 1: Создать `pyproject.toml`** по типовому образцу (`hatchling`, пакет `hub`, группа `dev`: pytest, pytest-asyncio, ruff, mypy; `asyncio_mode = "auto"`). Зависимости: `aiogram>=3,<4`, `aiosqlite`, `apscheduler>=3.10,<4`, `anthropic`, `pydantic-settings`, `tzdata`. `uv lock`.
- [ ] **Step 2: Написать падающие тесты**

```python
async def test_open_db_applies_migrations_once(tmp_path):
    db = await open_db(tmp_path / "t.db"); await db.close()
    db = await open_db(tmp_path / "t.db")
    rows = await (await db.execute("SELECT name FROM schema_migrations")).fetchall()
    assert [r["name"] for r in rows] == ["001_init.sql"]

async def test_entries_kind_is_checked(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute("INSERT INTO entries(owner_id,kind,title,created_at) VALUES (1,'spam','x','t')")

def test_settings_defaults(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "t"); monkeypatch.setenv("ANTHROPIC_API_KEY", "k"); monkeypatch.setenv("ADMIN_TG_ID", "1")
    s = Settings()
    assert (s.daily_tokens_in, s.daily_tokens_out, s.brief_time, s.tz) == (2_000_000, 200_000, "09:00", "Europe/Moscow")
```

- [ ] **Step 3: Run** `uv run pytest tests/test_db.py tests/test_config.py -v` → FAIL (нет модулей).
- [ ] **Step 4: Реализовать `config.py`, `clock.py`, `db.py`, `001_init.sql`.**
- [ ] **Step 5: Run** тот же набор → PASS; `uv run ruff check .` → чисто.
- [ ] **Step 6: Commit** `feat: каркас, конфиг, база и миграции`

---

### Task 2: Пользователи, приглашения, пространство «Семья»

**Files:**
- Create: `hub/models.py`, `hub/users.py`
- Test: `tests/test_users.py`

**Interfaces:**
- Consumes: `open_db`, `Clock` (Task 1).
- Produces:
  - `@dataclass(frozen=True) class User: id: int; tg_id: int; name: str; role: Literal["admin","user"]; brief_time: str`
  - `async def ensure_admin(db, tg_id: int, name: str, clock) -> User` — создаёт админа при первом запуске (по `Settings.admin_tg_id`), повторный вызов возвращает того же.
  - `async def get_user_by_tg(db, tg_id: int) -> User | None`
  - `async def create_invite(db, admin: User, clock) -> str` — код `secrets.token_urlsafe(16)`, срок **24 часа** (§20.1); не-админ → `PermissionError`.
  - `async def redeem_invite(db, code: str, tg_id: int, name: str, clock) -> User | None` — `None`, если кода нет, он просрочен или уже использован; уже зарегистрированный `tg_id` возвращает существующего пользователя, не тратя код. Новый пользователь автоматически становится участником пространства «Семья».
  - `async def family_space_id(db) -> int` — создаёт пространство `"Семья"` при первом обращении.
  - `async def list_users(db) -> list[User]`

- [ ] **Step 1: Написать падающие тесты**

```python
async def test_invite_once(db, clock):
    admin = await ensure_admin(db, 100, "Owner", clock)
    code = await create_invite(db, admin, clock)
    partner = await redeem_invite(db, code, 200, "Partner", clock)
    assert partner and partner.role == "user"
    assert await redeem_invite(db, code, 300, "Чужой", clock) is None
    assert await get_user_by_tg(db, 300) is None

async def test_invite_expires_after_24h(db, clock):
    admin = await ensure_admin(db, 100, "Owner", clock)
    code = await create_invite(db, admin, clock)
    later = FixedClock(clock.now() + timedelta(hours=24, seconds=1))
    assert await redeem_invite(db, code, 200, "Partner", later) is None

async def test_only_admin_invites(db, clock): ...   # PermissionError для role="user"

async def test_new_user_joins_family(db, clock): ... # space_members содержит нового пользователя
```

- [ ] **Step 2: Run** `uv run pytest tests/test_users.py -v` → FAIL.
- [ ] **Step 3: Реализовать `models.py` (User, а также `EntryKind = Literal["task","project","think","note"]`, `EntryStatus`) и `users.py`.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: пользователи и приглашения`

---

### Task 3: Слой доступа и шаринг

**Files:**
- Create: `hub/access.py`
- Test: `tests/test_access.py`

**Interfaces:**
- Consumes: `User` (Task 2).
- Produces:
  - `def visible_entries_sql(user_id: int) -> tuple[str, list]` — SQL-условие для `entries AS e` и параметры. Запись видна, если: `e.owner_id = user`; **или** есть `shares('entry', e.id)` на пользователя или на пространство, где он участник; **или** запись лежит в разделе, расшаренном пользователю или его пространству (выбор строителя: шаринг раздела открывает всё в нём — так работает «раздел Отпуск для Насти»; спека §20.8–20.9).
  - `def visible_sections_sql(user_id: int) -> tuple[str, list]` — то же для `sections AS s`.
  - `async def can_see_entry(db, user_id: int, entry_id: int) -> bool`
  - `async def share(db, owner: User, object_type: Literal["entry","section"], object_id: int, principal_type: Literal["user","space"], principal_id: int, clock) -> None` — делиться может только владелец объекта, иначе `PermissionError`; повтор — без ошибки.
  - `async def unshare(db, owner: User, object_type, object_id, principal_type, principal_id) -> None`
  - `async def shared_with(db, object_type, object_id) -> list[tuple[str, int]]`

- [ ] **Step 1: Добавить в `conftest.py` фикстуру `users`** (`a` — админ, `b` — по приглашению, оба в «Семье»; `c` — пользователь, вручную исключённый из `space_members`), затем **написать падающие тесты**

```python
async def test_private_by_default(db, users): # запись А не видна Б
    assert not await can_see_entry(db, users.b.id, entry_a)

async def test_share_to_user(db, users, clock):
    await share(db, users.a, "entry", entry_a, "user", users.b.id, clock)
    assert await can_see_entry(db, users.b.id, entry_a)
    assert not await can_see_entry(db, users.c.id, entry_a)

async def test_share_to_family_space(...):  # видят Б (участник), не видит В
async def test_shared_section_opens_its_entries(...):  # запись в разделе, раздел расшарен Б → Б видит запись
async def test_only_owner_shares(...):  # Б не может расшарить запись А → PermissionError
async def test_unshare_closes_access(...):
```

- [ ] **Step 2: Run** `uv run pytest tests/test_access.py -v` → FAIL.
- [ ] **Step 3: Реализовать `access.py`.** Условие собирается одной строкой с `EXISTS`-подзапросами; параметры — позиционные.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: слой доступа и шаринг`

---

### Task 4: Записи, задачи, подзадачи, разделы

**Files:**
- Create: `hub/entries.py`, `hub/sections.py`
- Test: `tests/test_entries.py`, `tests/test_sections.py`

**Interfaces:**
- Consumes: `access.visible_entries_sql`, `access.visible_sections_sql`, `can_see_entry` (Task 3).
- Produces:
  - `@dataclass class Entry: id; owner_id; kind: EntryKind; title: str; body: str; due_date: date | None; contour: str | None; status: EntryStatus; parent_id: int | None; section_id: int | None; created_at: datetime; done_at: datetime | None`
  - `async def create_entry(db, owner: User, kind: EntryKind, title: str, body: str = "", due_date: date | None = None, contour: str | None = None, section_id: int | None = None, parent_id: int | None = None, clock=...) -> Entry` — `section_id` и `parent_id` должны быть видны владельцу, иначе `PermissionError`.
  - `async def set_kind(db, user: User, entry_id: int, kind: EntryKind) -> Entry` — только владелец.
  - `async def set_section(db, user: User, entry_id: int, section_id: int | None) -> Entry`
  - `async def complete(db, user: User, entry_id: int, clock) -> Entry` — может любой, кто видит запись (общие задачи закрывает любой участник; выбор строителя); повторный вызов возвращает запись без изменений `done_at`; нет доступа → `PermissionError`.
  - `async def list_open_tasks(db, user: User, today: date) -> list[Entry]` — видимые `kind='task'`, `status='open'`, сортировка: просроченные, сегодня, с датой по возрастанию, без даты.
  - `async def list_think(db, user: User) -> list[Entry]` — видимые `kind='think'`, `status='open'`, старые первыми.
  - `async def children(db, user: User, parent_id: int) -> list[Entry]`
  - `sections.py`: `@dataclass class Section: id; owner_id; name; archived_at`; `async def get_or_create_section(db, owner: User, name: str) -> Section` (имя сравнивается без учёта регистра и пробелов по краям); `async def list_sections(db, user: User) -> list[Section]` (видимые, неархивные); `async def archive_section(db, owner: User, section_id: int, clock) -> None`.
- Контуры задач — фиксированный список `CONTOURS = ("личное","работа","проект","хобби","финансы","быт")` (§9); неизвестный контур → `None`.

- [ ] **Step 1: Написать падающие тесты:** `test_list_open_tasks_order` (просрочено → сегодня → позже → без даты), `test_complete_twice_keeps_done_at`, `test_complete_shared_task_by_member`, `test_complete_hidden_task_denied`, `test_subtasks_listed_under_project`, `test_section_name_case_insensitive` («отпуск » и «Отпуск» — один раздел), `test_archived_section_hidden`, `test_unknown_contour_becomes_none`.
- [ ] **Step 2: Run** `uv run pytest tests/test_entries.py tests/test_sections.py -v` → FAIL.
- [ ] **Step 3: Реализовать `entries.py`, `sections.py`.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: записи, задачи и разделы`

---

### Task 5: Маскирование, бюджет, обёртка LLM

**Files:**
- Create: `hub/pii.py`, `hub/budget.py`, `hub/llm.py`, `tests/fakes.py`
- Test: `tests/test_pii.py`, `tests/test_budget.py`, `tests/test_llm.py`

**Interfaces:**
- Produces:
  - `def mask_pii(text: str) -> str` — заменяет на `[номер скрыт]`: номера карт (13–19 цифр подряд или группами по 4 через пробел/дефис) и паспорт РФ (`\b\d{2}\s?\d{2}\s?\d{6}\b`). Даты, суммы, телефоны не трогает.
  - `class BudgetExceeded(Exception)`
  - `async def check_budget(db, user_id: int, day: date, settings) -> None` — бросает `BudgetExceeded`, если `tokens_in >= daily_tokens_in` или `tokens_out >= daily_tokens_out`.
  - `async def record_usage(db, user_id: int, day: date, tokens_in: int, tokens_out: int) -> None` — upsert-сложение.
  - `@dataclass class Classification: kind: EntryKind; title: str; due_date: date | None; contour: str | None; section: str | None`
  - `class Llm(Protocol)`: `async def classify(self, user_id: int, text: str, today: date, sections: list[str]) -> Classification`; `async def decompose(self, user_id: int, title: str, body: str) -> list[str]`; `async def brief_intro(self, user_id: int, facts_text: str) -> str`.
  - `class AnthropicLlm(Llm)` — `AsyncAnthropic(api_key=...)`. Каждый вызов: `check_budget` → `mask_pii` на входной текст → запрос с `metadata={"user_id": f"personal-hub:{user_id}"}` → `record_usage` по `response.usage`. `classify` — модель `model_fast`, принудительный вызов инструмента `save_entry` с JSON-схемой (`kind` enum из четырёх, `title` ≤ 120 символов, `due_date` `YYYY-MM-DD` или null, `contour` enum из `CONTOURS` или null, `section` — одно из переданных имён или null); в промпт передаются сегодняшняя дата и день недели по локальному времени. `decompose` — `model_smart`, инструмент `subtasks` (массив 3–7 строк). `brief_intro` — `model_smart`, `max_tokens=300`, текст без списков.
  - Ошибки: невалидный ответ инструмента, неизвестный `kind`, исключение SDK → `LlmError`. `BudgetExceeded` пробрасывается как есть.
  - `tests/fakes.py`: `class FakeLlm(Llm)` со списками заготовленных ответов, флагом `fail=True` и списком `calls` (словари аргументов каждого вызова, ключи как у параметров: `user_id`, `text`, `today`, …); `class RecordingClient` — заглушка `AsyncAnthropic`, сохраняет `kwargs` последнего `messages.create` и возвращает ответ с одним `tool_use` блоком `tool_input` и `usage(input_tokens=10, output_tokens=5)`.

- [ ] **Step 1: Написать падающие тесты**

```python
def test_mask_card_and_passport():
    assert mask_pii("карта 4276 1234 5678 9012, паспорт 03 15 123456") == "карта [номер скрыт], паспорт [номер скрыт]"
def test_mask_keeps_dates_money_phone():
    s = "до 14.03.2027 оплатить 8 400 ₽, звонить +7 918 123-45-67"
    assert mask_pii(s) == s

async def test_budget_blocks_after_limit(db, settings):
    await record_usage(db, 1, date(2026,11,2), 2_000_000, 0)
    with pytest.raises(BudgetExceeded): await check_budget(db, 1, date(2026,11,2), settings)
    await check_budget(db, 1, date(2026,11,3), settings)  # новый день — снова можно

async def test_anthropic_llm_sends_metadata_and_masks(db, settings, clock):
    client = RecordingClient(tool_input={"kind": "note", "title": "x", "due_date": None, "contour": None, "section": None})
    await AnthropicLlm(settings, db, clock, client=client).classify(7, "карта 4276 1234 5678 9012", date(2026,11,2), [])
    assert client.kwargs["metadata"] == {"user_id": "personal-hub:7"}
    assert "4276" not in str(client.kwargs["messages"])
async def test_classify_rejects_unknown_kind(...):  # ответ инструмента kind="spam" → LlmError
```

- [ ] **Step 2: Run** `uv run pytest tests/test_pii.py tests/test_budget.py tests/test_llm.py -v` → FAIL.
- [ ] **Step 3: Реализовать `pii.py`, `budget.py`, `llm.py`, `fakes.py`.** Для теста `AnthropicLlm` клиент передаётся в конструктор (`AnthropicLlm(settings, db, clock, client=...)`), в тесте — заглушка, записывающая аргументы `messages.create`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: маскирование, бюджет и обёртка Anthropic`

---

### Task 6: Сервисный слой — регистрация, приглашения, ввод записи

**Files:**
- Create: `hub/service.py`
- Test: `tests/test_service_intake.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces:
  - `@dataclass class Button: text: str; data: str` (`data` ≤ 64 байт — лимит Telegram).
  - `@dataclass class Reply: text: str; buttons: list[list[Button]] = field(default_factory=list)`
  - `class Service(db, llm: Llm, clock, settings)`:
    - `async def start(self, tg_id: int, name: str, payload: str | None) -> Reply | None` — зарегистрированному: приветствие; новому с верным кодом: регистрирует, приветствие содержит фразу о том, что администратор сервера технически может видеть данные (§20.1); иначе `None` (молчание).
    - `async def invite(self, tg_id: int, bot_username: str) -> Reply | None` — админу ссылка `https://t.me/<bot_username>?start=<code>` и «действует 24 часа»; остальным `None`.
    - `async def text(self, tg_id: int, text: str) -> Reply | None` — незарегистрированный → `None` и никаких записей. Иначе `classify` → `create_entry`; ответ вида `📌 Задача: <title> · до 07.11 · раздел Отпуск` и кнопки: смена типа (`Задача`/`Проект`/`Подумать`/`Заметка`, data `k:<id>:<kind>`), `Раздел…` (`s:<id>`), `Поделиться` (`sh:<id>`), `✔` для задачи (`d:<id>`). Раздел из `Classification.section` берётся только если такой раздел виден пользователю, иначе без раздела.
    - Если `classify` бросил `LlmError` → запись `kind='note'`, `title` = первые 120 символов текста, `body` = весь текст; ответ «Не разобрал, сохранил как заметку» с теми же кнопками смены типа.
    - Если `BudgetExceeded` → то же, что `LlmError`, плюс строка «дневной лимит модели исчерпан».
    - Для `kind='project'` после сохранения вызывается `decompose`, и ответ содержит пронумерованные подзадачи с кнопками `✅ Создать подзадачи` (`pj:<id>:ok`) и `Не нужно` (`pj:<id>:no`); предложенный список хранится в `entries.body` проекта блоком `Подзадачи (предложение):` — выбор строителя, чтобы не заводить таблицу ради черновика.
    - `async def callback(self, tg_id: int, data: str) -> Reply | None` — разбирает `k:`, `s:`, `sh:`, `d:`, `pj:`; чужая запись без доступа → `Reply("Нет доступа")`.

- [ ] **Step 1: Написать падающие тесты:** `test_stranger_is_ignored_and_nothing_saved`, `test_stranger_with_used_code_is_ignored`, `test_start_greeting_mentions_admin_access`, `test_text_creates_task_with_due_and_section`, `test_llm_failure_saves_note`, `test_budget_exceeded_saves_note_and_says_so`, `test_project_proposes_subtasks_and_creates_on_ok` (после `pj:<id>:ok` у проекта 3 подзадачи `kind='task'`, `parent_id` = проект), `test_change_kind_by_button`, `test_callback_on_hidden_entry_says_no_access`, `test_button_data_under_64_bytes`.
- [ ] **Step 2: Run** `uv run pytest tests/test_service_intake.py -v` → FAIL.
- [ ] **Step 3: Реализовать `service.py`** (без Telegram-импортов).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: сервисный слой ввода записей`

---

### Task 7: Сроки «до пятницы» и часовой пояс

**Files:**
- Modify: `hub/llm.py` (промпт `classify`), `hub/service.py`
- Test: `tests/test_dates.py`

**Interfaces:**
- Produces: `def resolve_due(raw: date | None, today: date) -> date | None` в `hub/entries.py` — отбрасывает даты в прошлом более чем на 1 день (модель ошиблась годом: переносит на следующий год, если месяц-день ещё впереди, иначе `None`).
- `Service.text` передаёт в `classify` `today = clock.today()` (локальная), а не дату сервера.

- [ ] **Step 1: Написать падающие тесты**

```python
async def test_today_is_moscow_date_after_utc_midnight(db, users, settings):
    # 7 ноября 01:30 по локальному времени = 6 ноября 22:30 UTC: «сегодня» должно быть 7-е
    clock = FixedClock(datetime(2026, 11, 7, 1, 30, tzinfo=ZoneInfo("Europe/Moscow")))
    llm = FakeLlm(classify=[Classification("task", "купить билеты", date(2026, 11, 13), None, None)])
    await Service(db, llm, clock, settings).text(users.a.tg_id, "купить билеты до пятницы")
    assert llm.calls[0]["today"] == date(2026, 11, 7)

def test_resolve_due_wrong_year():
    assert resolve_due(date(2025, 12, 1), date(2026, 11, 2)) == date(2026, 12, 1)
def test_resolve_due_far_past_dropped():
    assert resolve_due(date(2025, 3, 1), date(2026, 11, 2)) is None
```

- [ ] **Step 2: Run** `uv run pytest tests/test_dates.py -v` → FAIL.
- [ ] **Step 3: Реализовать `resolve_due`, вызвать его в `Service.text` после `classify`.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `fix: сроки по московской дате`

---

### Task 8: Команды `/tasks`, `/sections`, `/section`, шаринг кнопками

**Files:**
- Modify: `hub/service.py`
- Test: `tests/test_service_commands.py`

**Interfaces:**
- Produces в `Service`:
  - `async def tasks(self, tg_id: int) -> Reply | None` — до 20 задач из `list_open_tasks`; строка `⚠️` для просроченных, `•` для остальных, рядом срок `дд.мм` и пометка `👥`, если задача чужая или расшарена; кнопки `✔ <№>` по 4 в ряд. Больше 20 — строка «и ещё N».
  - `async def sections(self, tg_id: int) -> Reply | None` — список видимых разделов с числом открытых задач.
  - `async def section_command(self, tg_id: int, name: str) -> Reply | None` — `/section Отпуск`: создаёт или находит раздел и переносит в него **последнюю запись пользователя за 10 минут** (выбор строителя); без записи — просто создаёт раздел.
  - Колбэк `sh:<id>` → кнопки получателей: каждый другой пользователь (`shu:<id>:<user_id>`) и `Семья` (`shs:<id>:<space_id>`); колбэк `s:<id>` → кнопки разделов пользователя (`ss:<id>:<section_id>`) + `Без раздела`.
  - Повторный `d:<id>` → «Уже сделано», без ошибок.

- [ ] **Step 1: Написать падающие тесты:** `test_tasks_shows_overdue_first_and_shared_mark`, `test_tasks_caps_at_20`, `test_double_done_is_idempotent`, `test_share_buttons_list_other_users_and_family`, `test_share_then_other_user_sees_in_tasks`, `test_section_command_moves_last_entry_within_10_min`, `test_section_command_ignores_older_entry`.
- [ ] **Step 2: Run** `uv run pytest tests/test_service_commands.py -v` → FAIL.
- [ ] **Step 3: Реализовать.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: команды задач, разделов и шаринг`

---

### Task 9: Брифинг

**Files:**
- Create: `hub/brief.py`
- Test: `tests/test_brief.py`

**Interfaces:**
- Consumes: `list_open_tasks`, `list_think`, `Llm.brief_intro`.
- Produces:
  - `@dataclass class BriefFacts: day: date; overdue: list[Entry]; today: list[Entry]; upcoming: list[Entry]; think: list[Entry]` — `upcoming` = задачи со сроком в ближайшие 7 дней; `think` = 1–2 самые старые мысли **только по понедельникам** (§20.4 «раз в неделю», выбор строителя).
  - `async def collect(db, user: User, day: date) -> BriefFacts`
  - `def render_lists(facts: BriefFacts) -> str` — разделы по порядку §10 (календарь, сроки и почта появятся в 1b–2): «Горит» (просроченные и сегодня), «На неделе», «Подумать». Даты и названия — только из `facts`, модель их не пишет (§10). Пустой день → «Задач на сегодня нет».
  - `async def build(db, llm, user: User, day: date) -> tuple[str, bool]` — `(текст, raw)`. Текст = вступление `brief_intro` (1–2 предложения) + `render_lists`. При `LlmError`/`BudgetExceeded` → только `render_lists`, `raw=True`. Итог обрезается до 4096 символов с последней строкой «…ещё N пунктов — /tasks».

- [ ] **Step 1: Написать падающие тесты:** `test_collect_groups_overdue_today_upcoming`, `test_think_only_on_monday`, `test_render_uses_db_dates_only` (в тексте `07.11` из записи, и вступление модели не попадает в списки), `test_build_falls_back_to_raw_on_llm_error`, `test_build_truncates_to_4096`, `test_brief_includes_shared_tasks_with_mark`.
- [ ] **Step 2: Run** `uv run pytest tests/test_brief.py -v` → FAIL.
- [ ] **Step 3: Реализовать `brief.py`.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: брифинг по задачам`

---

### Task 10: Расписание, повторы, сторож, локальный бэкап

**Files:**
- Create: `hub/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `brief.build`, таблица `briefs`.
- Produces:
  - `async def run_brief(db, llm, send: Callable[[int, str], Awaitable[None]], user: User, day: date, force: bool = False) -> str` — статус в `briefs`; `force=True` шлёт всегда (в том числе сырой брифинг сразу) и `briefs` не трогает. Повторный вызов за тот же день после `sent`/`raw` ничего не шлёт (защита от двойного брифинга). Если `build` вернул `raw=True` и это не последняя попытка — не шлёт, ставит `pending`, `attempts += 1`; повторы через **15 минут, до 4 раз** (§10); на 4-й попытке шлёт сырой брифинг со статусом `raw`.
  - `async def watchdog(db, send, admin: User, day: date, users: list[User]) -> None` — в **9:15** (для пользователей с другим `brief_time` — через 15 минут после их времени) у кого нет `sent`/`raw` → админу «брифинг для <имя> не ушёл: <error>» (§14).
  - `def build_scheduler(...) -> AsyncIOScheduler` — `CronTrigger` на `brief_time` каждого пользователя в `Europe/Moscow`; задача повторов; сторож; бэкап в **03:30**: `sqlite3` `backup()` в `backup_dir/hub-YYYY-MM-DD.db`, хранить **7** последних (выбор строителя: в облако бэкап уходит в 1b).
  - `async def Service.brief(self, tg_id: int) -> Reply | None` — `/brief`: собирает `brief.build` и возвращает текст как `Reply` (вне расписания, `briefs` не трогает); незарегистрированному `None`.

- [ ] **Step 1: Написать падающие тесты:** `test_brief_sent_once_per_day`, `test_raw_retries_then_sends_raw_on_4th`, `test_watchdog_reports_missing_brief`, `test_watchdog_quiet_when_sent`, `test_backup_keeps_last_7`, `test_force_brief_ignores_sent_status`.
- [ ] **Step 2: Run** `uv run pytest tests/test_scheduler.py -v` → FAIL.
- [ ] **Step 3: Реализовать `scheduler.py`; добавить `Service.brief`.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: расписание брифингов, сторож и бэкап`

---

### Task 11: Telegram-бот и точка входа

**Files:**
- Create: `hub/bot.py`, `hub/__main__.py`
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `Service`, `build_scheduler`.
- Produces:
  - `def reply_markup(reply: Reply) -> InlineKeyboardMarkup | None`
  - `def build_router(service: Service, bot_username: str) -> Router` — хендлеры: `/start [payload]`, `/invite`, `/tasks`, `/sections`, `/section <имя>`, `/brief`, `/help`, текст, `callback_query`. Только личные чаты (`F.chat.type == "private"`; группы — в 1d). Ответ `None` → бот ничего не отправляет; на колбэк — пустой `answer()`.
  - `__main__.py`: `Settings()` → `open_db` → `ensure_admin(settings.admin_tg_id)` → `AnthropicLlm` → `Bot(parse_mode=None)` (текст без разметки, чтобы `_` и `*` в задачах не ломали сообщения; выбор строителя) → `Dispatcher` с роутером → `build_scheduler(...).start()` → `dp.start_polling(bot)`. Логи в stdout одной строкой JSON.
- [ ] **Step 1: Написать падающие тесты:** `test_reply_markup_rows`, `test_none_reply_sends_nothing` (хендлер текста с подменённым `Service`, возвращающим `None`, не вызывает `message.answer`), `test_group_chat_ignored`.
- [ ] **Step 2: Run** `uv run pytest tests/test_bot.py -v` → FAIL.
- [ ] **Step 3: Реализовать `bot.py`, `__main__.py`.**
- [ ] **Step 4: Run** весь набор `uv run pytest -v` → PASS; `uv run ruff check .` → чисто.
- [ ] **Step 5: Commit** `feat: Telegram-бот и запуск`

---

### Task 12: Docker, деплой на VPS, дымовая проверка

**Files:**
- Create: `Dockerfile`, `compose.yml`, `.env.example`, `ops/DEPLOY.md`

**Interfaces:**
- `Dockerfile` — по типовому образцу: `python:3.12-slim`, `uv` фиксированной версии, `uv sync --frozen --no-dev`, пользователь без root (uid 10001), `TZ=Europe/Moscow`, `CMD ["python","-m","hub"]`.
- `compose.yml` — сервис `hub`, `restart: unless-stopped`, `mem_limit: 300m`, `env_file: .env`, том `./data:/data`, **без портов и без внешней сети reverse proxy** (веба в 1a нет).
- `.env.example` — `BOT_TOKEN=`, `ANTHROPIC_API_KEY=`, `ADMIN_TG_ID=`.
- `ops/DEPLOY.md` — шаги ниже, плюс откат: `docker compose down`, данные в `/opt/personal-hub/data` остаются.

- [ ] **Step 1: Владелец (руками):** создаёт бота у @BotFather, присылает в чат **только** имя бота и свой Telegram ID (не токен); токен вставляет в `.env` на сервере сам или кладёт в локальный файл вне репозитория, откуда его копирует деплой-шаг, не выводя в лог.
- [ ] **Step 2: Собрать образ локально** `docker build -t personal-hub .` → успех; `docker run --rm personal-hub python -c "import hub"` → без ошибок.
- [ ] **Step 3: На сервере** (`ssh <user>@<VPS_IP>`): `/opt/personal-hub/` с `compose.yml`, `.env` (права 600); `ANTHROPIC_API_KEY` переносится из env-файла другого проекта на сервере (если ключ общий) командой без вывода значения; `data/` с владельцем uid 10001.
- [ ] **Step 4: Запуск** `docker compose up -d --build` → `docker compose ps` показывает `running`; `docker stats --no-stream` — память `hub` < 150 МБ; `free -m` — `available` не ниже 1 ГБ.
- [ ] **Step 5: Дымовая проверка с владельцем:** `/start` → приветствие; «купить билеты до пятницы» → задача с ближайшей пятницей; «переоформить машину» → проект с подзадачами, `✅` создаёт их; `/invite` → ссылка; `/brief` → брифинг пришёл; сообщение с другого аккаунта без приглашения → молчание; `docker compose logs hub` без ошибок.
- [ ] **Step 6: Commit** `ops: Docker и деплой на VPS`

---

## Что в 1a сознательно не делаем

`/ask` и поиск по мозгу, `/think`-диалог, реестр и облако, календарь, веб и «Настройки», семейная группа, голос, Notion, почта, MCP — по таблице нарезки выше. Брифинг в 1a — только задачи и «Подумать».

## Критерий готовности 1a

Из §15 и §20.6, применимое к 1a: 7 дней подряд брифинг приходит владельцу в 9:00; партнёр подключён по приглашению и не видит нерасшаренных записей владельца, а расшаренную задачу видит и может закрыть; 10 записей подряд разложены по типам без правки в 8 из 10.
