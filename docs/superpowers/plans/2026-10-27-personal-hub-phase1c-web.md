# Personal Hub — фаза 1c «Веб-дашборд, вход, календари» Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Веб-дашборд `desk.example.com` (только из VPN) в стиле A: вход по ссылке из бота и через мини-приложение Telegram, экраны «Сегодня», «Задачи», «Настройки», подключение календарей Google/Яндекс по секретной ссылке iCal и события календаря в утреннем брифинге.

**Architecture:** FastAPI + Jinja2 + HTMX в том же процессе и event loop, что бот и планировщик (uvicorn `Server.serve()` рядом с polling). Вся логика доступа — прежняя (`hub/access.py`, `entries.py`, `Service`); веб — тонкий слой над ней, как бот. CSS — daisyUI 5 + Tailwind 4, собирается standalone-бинарником при сборке Docker-образа. Календари — периодическая загрузка ICS, разбор в таблицу событий.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, HTMX 2 (статикой), daisyUI 5 + Tailwind 4 standalone CLI, `httpx`, `icalendar` + `recurring-ical-events`, `cryptography` (Fernet), pytest + `httpx.ASGITransport`.

**Spec:** `docs/superpowers/specs/2026-10-26-personal-hub-design.md` — **§21 главнее** §12 и §20 по дашборду; §20.1 (доступ), §20.2 (подключения), §20.12 (настройки). Стиль и токены: `docs/research/2026-10-26-dashboard-research.md` («Решение владельца 27.10: стиль A»)..

## Global Constraints

- Все правила фазы 1a в силе (Python 3.12, uv, часовой пояс, модели, маскирование, бюджет, секреты в `.env`, ничего не удаляется безвозвратно, бот молчит незнакомцам).
- Один процесс: веб, бот и планировщик в одном event loop; одна база SQLite; лимит памяти контейнера поднимается до **384 МБ** (`mem_limit: 384m`) — сейчас 204 МБ без веба.
- **Видимость данных — только через `hub/access.py`** и существующие функции `entries`/`sections`; веб-обработчики своих SQL-правил доступа не пишут.
- Всё на одном origin `https://desk.example.com`; ответы с `Content-Security-Policy: frame-ancestors 'self' https://web.telegram.org; default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
- Сессия: случайный токен 32 байта (`secrets.token_urlsafe(32)`), в базе только **SHA-256** от токена; cookie `hub_session`, `HttpOnly; Secure; SameSite=Lax; Path=/`, срок **30 дней**; тот же токен принимается в `Authorization: Bearer`. Ссылка входа: одноразовая, **10 минут**, в базе тоже хеш.
- Изменяющие запросы (POST/DELETE) принимаются только с заголовком `HX-Request: true` или с `Authorization: Bearer …` — защита от межсайтовых форм (CSRF).
- `initData` мини-приложения: HMAC-SHA256, ключ `HMAC_SHA256(key="WebAppData", msg=bot_token)`, сравнение `hmac.compare_digest`, `auth_date` не старше **3600 с**, пользователь должен быть зарегистрирован.
- Секреты подключений (ссылки iCal) — шифрование Fernet ключом `CONNECTIONS_KEY` из `.env`; в логи и ответы не выводятся (в UI — только хост ссылки и «●●●●»).
- Загрузка ICS: только `https://`, хост не резолвится в частные/loopback/link-local/зарезервированные адреса (проверка каждого адреса DNS-ответа), таймаут **20 с**, размер ответа не больше **2 МБ**, не больше **3** редиректов (каждый проверяется так же).
- Стиль A: токены из исследования; шрифт Inter (Google Fonts), база 14 px, заголовок 24/600, подписи разделов 12 px капсом 0.06em, скругления 10/8/6, зоны нажатия ≥ 44 px, контраст текста ≥ 4.5:1. Цвета — CSS-переменные с цепочкой `var(--tg-theme-…, <цвет A>)`; тёмная тема — `prefers-color-scheme` и `Telegram.WebApp.colorScheme`.
- Никаких внешних JS, кроме `telegram-web-app.js` (`https://telegram.org/js/telegram-web-app.js`, добавить в CSP `script-src 'self' https://telegram.org`); HTMX — локальный файл в `static/` (версия закреплена, sha256 проверяется в скрипте загрузки).

## Review Focus

1. **Пересланная или старая ссылка входа** — второй переход по ней и переход через 10 минут ничего не дают («ссылка устарела, попроси новую в боте /web»). Тест в Task 2.
2. **Поддельный или протухший `initData`** (подпись от другого бота, `auth_date` суточной давности, незарегистрированный пользователь) — 401, сессия не создаётся. Тест в Task 2.
3. **Подбор чужих id в веб-запросах** (`POST /entries/{id}/done` на чужую нерасшаренную задачу, `GET /connections/{id}` чужого подключения) — «Нет доступа»/404, никаких данных. Тесты в Task 5 и Task 7.
4. **Ссылка iCal на внутренний адрес** (`https://<VPN_HOST>/…`, `https://localhost/…`, `http://…`, редирект на `169.254.169.254`) — отказ до запроса, статус подключения не меняется на «работает». Тест в Task 6.
5. **Огромный или битый ICS** (5 МБ, мусор, событие без даты) — подключение получает статус `error` с понятной причиной, процесс не падает, старые события не стираются. Тест в Task 6.

---

## File Structure

```
migrations/002_web.sql           sessions, login_links, connections, calendar_events
hub/crypto.py                    Fernet: encrypt/decrypt секретов подключений
hub/auth.py                      ссылки входа, сессии, проверка initData (без FastAPI)
hub/connections.py               подключения: создать/список/статус/удалить (с доступом)
hub/calendar_ics.py              безопасная загрузка ICS, разбор, события дня
hub/web/__init__.py              create_app(...) -> FastAPI
hub/web/deps.py                  current_user, require_htmx_or_bearer, шаблоны
hub/web/routes_auth.py           /auth/link, /auth/tma, /logout
hub/web/routes_pages.py          /, /tasks, /settings, /mail, /docs + частичные шаблоны
hub/web/routes_actions.py        POST: записи, задачи, подключения, настройки
hub/web/templates/               base.html, today.html, tasks.html, settings.html, stub.html, partials/*.html
hub/web/static/                  app.css (собирается), htmx.min.js, app.js (Telegram + токен)
styles/app.css                   исходник Tailwind/daisyUI: тема A light/dark, токены
scripts/fetch_assets.sh          скачивает Tailwind CLI, daisyui.mjs, htmx — по версии и sha256
tests/test_crypto.py, test_auth.py, test_web_*.py, test_connections.py, test_calendar_ics.py
```

Граница: `hub/web/*` не импортирует `aiosqlite`-запросы напрямую — только функции из `hub/*` и `Service`.

---

### Task 1: Миграция 002, атомарные миграции, шифрование, настройки

**Files:** Create `migrations/002_web.sql`, `hub/crypto.py`, `tests/test_crypto.py`; Modify `hub/db.py`, `hub/config.py`, `.env.example`, `pyproject.toml`; Test `tests/test_db.py`, `tests/test_config.py`.

**Interfaces — Produces:**
- `002_web.sql`:
  - `sessions(token_hash TEXT PK, user_id INTEGER NOT NULL REFERENCES users, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_seen_at TEXT, via TEXT NOT NULL CHECK(via IN ('link','tma')))`
  - `login_links(token_hash TEXT PK, user_id INTEGER NOT NULL REFERENCES users, expires_at TEXT NOT NULL, used_at TEXT)`
  - `connections(id INTEGER PK, owner_id INTEGER NOT NULL REFERENCES users, kind TEXT NOT NULL CHECK(kind IN ('ics_calendar')), name TEXT NOT NULL, secret_enc BLOB NOT NULL, hint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','ok','error','auth_expired','disabled')), last_sync_at TEXT, last_success_at TEXT, last_error TEXT, created_at TEXT NOT NULL)` — `hint` = хост ссылки для показа.
  - `calendar_events(connection_id INTEGER NOT NULL REFERENCES connections ON DELETE CASCADE, uid TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT, all_day INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL, location TEXT, PRIMARY KEY(connection_id, uid, starts_at))`
- `hub/db.py`: каждая миграция применяется и записывается в `schema_migrations` **в одной транзакции** (`BEGIN` → statements → `INSERT INTO schema_migrations` → `COMMIT`; при ошибке `ROLLBACK`); разбор файла на statements — `sqlite3.complete_statement` построчно (закрывает отложенное замечание Task 1 фазы 1a).
- `hub/crypto.py`: `class SecretBox: __init__(key: str)`; `seal(plain: str) -> bytes`; `open(token: bytes) -> str` (`InvalidToken` → `ValueError("секрет не расшифровать")`).
- `Settings`: `web_base_url: str = "https://desk.example.com"`, `web_port: int = 8080`, `connections_key: str` (обязательный; `.env.example` с подсказкой `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`), `session_days: int = 30`.
- Зависимости: `fastapi`, `uvicorn[standard]`, `jinja2`, `httpx`, `icalendar`, `recurring-ical-events`, `cryptography`, `python-multipart`.

- [ ] **Step 1: Падающие тесты:** `test_migrations_002_applied_and_recorded`, `test_failed_migration_rolls_back_and_is_not_recorded` (временная папка миграций с битым `003_bad.sql` → таблиц из него нет, записи нет, `open_db` бросает), `test_secretbox_roundtrip`, `test_secretbox_wrong_key_raises_valueerror`, `test_settings_requires_connections_key`.
- [ ] **Step 2: Run** `uv run pytest tests/test_db.py tests/test_crypto.py tests/test_config.py -q` → FAIL.
- [ ] **Step 3: Реализовать.** `MIGRATIONS_DIR` можно подменять в тестах (параметр `open_db(path, migrations_dir=MIGRATIONS_DIR)`).
- [ ] **Step 4: Run** весь набор → PASS.
- [ ] **Step 5: Commit** `feat: миграция 002, атомарные миграции, шифрование секретов`

---

### Task 2: Вход — ссылки, сессии, initData

**Files:** Create `hub/auth.py`, `tests/test_auth.py`.

**Interfaces — Produces:**
- `async def create_login_link(db, user: User, clock) -> str` — сырой токен (`secrets.token_urlsafe(32)`), в базе SHA-256, срок 10 мин.
- `async def redeem_login_link(db, token: str, clock, session_days: int) -> tuple[str, User] | None` — атомарно: `UPDATE login_links SET used_at=? WHERE token_hash=? AND used_at IS NULL AND expires_at > ?` (сравнение моментов — разбором ISO в aware datetime, как в users.py), затем новая сессия (`via='link'`); иначе `None`.
- `async def create_session(db, user: User, via: Literal['link','tma'], clock, session_days: int) -> str`.
- `async def session_user(db, token: str, clock) -> User | None` — по хешу, непросроченная; обновляет `last_seen_at` не чаще раза в 10 минут.
- `async def revoke_session(db, token: str) -> None` (запись не удаляется: `expires_at = now`).
- `def verify_init_data(init_data: str, bot_token: str, now: datetime, max_age_s: int = 3600) -> int | None` — чистая функция: разбор query-string (`urllib.parse.parse_qsl(keep_blank_values=True)`), `data_check_string` = все пары кроме `hash`, по алфавиту ключа, `key=value` через `\n`; секрет = `HMAC_SHA256(key=b"WebAppData", msg=bot_token)`; `hmac.compare_digest`; проверка `auth_date`; возвращает `user.id` из JSON поля `user` или `None`.
- `async def tma_login(db, init_data: str, settings, clock) -> tuple[str, User] | None` — `verify_init_data` → `get_user_by_tg` → сессия `via='tma'`.

- [ ] **Step 1: Падающие тесты** (в тесте `initData` собирается настоящей подписью тем же алгоритмом): `test_link_single_use`, `test_link_expires_after_10_min`, `test_session_valid_until_30_days_then_none`, `test_revoked_session_none`, `test_init_data_valid`, `test_init_data_wrong_bot_token`, `test_init_data_tampered_field`, `test_init_data_too_old`, `test_tma_unknown_user_none`, `test_token_not_stored_in_plain` (в таблицах нет сырого токена).
- [ ] **Step 2: Run** `uv run pytest tests/test_auth.py -q` → FAIL.
- [ ] **Step 3: Реализовать `hub/auth.py`.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat: вход по ссылке и через initData, сессии`

---

### Task 3: Каркас веба, стиль A, сборка CSS

**Files:** Create `hub/web/__init__.py`, `hub/web/deps.py`, `hub/web/routes_auth.py`, `hub/web/templates/base.html`, `hub/web/templates/stub.html`, `hub/web/static/app.js`, `styles/app.css`, `scripts/fetch_assets.sh`, `tests/test_web_auth.py`; Modify `.gitignore` (`/.tools/`, `hub/web/static/app.css`, `hub/web/static/htmx.min.js`).

**Interfaces — Produces:**
- `def create_app(db, service: Service, settings, clock, box: SecretBox, http_client_factory: Callable[[], httpx.AsyncClient], on_brief_time_changed: Callable[[User], Awaitable[None]] | None = None) -> FastAPI` (`box` и `http_client_factory` нужны «Настройкам» в Task 7; в Task 3 только передаются в `app.state`) — монтирует `/static`, роутеры, middleware заголовков безопасности (Global Constraints), обработчик 401: для `HX-Request` — ответ 401 с `HX-Trigger: reauth`, иначе страница «Войдите: напишите боту /web».
- `deps.py`: `async def current_user(request) -> User` (сначала `Authorization: Bearer`, потом cookie `hub_session`; нет → 401); `async def require_write(request)` (Global Constraints про `HX-Request`/Bearer → иначе 403); `templates: Jinja2Templates` с фильтрами `ddmm` (дата `дд.мм`) и `hhmm`.
- `routes_auth.py`: `GET /auth/link?t=` → `redeem_login_link` → ставит cookie, редирект на `/`; неудача → страница «Ссылка устарела — попроси новую в боте /web». `POST /auth/tma` (JSON `{"init_data": str}`) → `{"token": str}` + cookie; 401 при неудаче. `POST /logout` → `revoke_session`, cookie снимается. `GET /healthz` → `{"ok": true}` без входа.
- `base.html`: `<html lang="ru" data-theme="hub-light">`, в `<head>` сначала `telegram-web-app.js`, затем `app.css`, Inter, `htmx.min.js`, `app.js`; шапка, контент, нижняя навигация (`< 1024px`: 4 вкладки «Сегодня · Задачи · Почта · Документы», настройки — иконкой в шапке) и боковое меню (`≥ 1024px`); активная вкладка — переменная шаблона `tab`.
- `styles/app.css`: `@import "tailwindcss"; @plugin "./daisyui.mjs";` + две темы daisyUI `hub-light`/`hub-dark` (цвета из токенов A: `--color-base-100` карточка, `--color-base-200` фон, `--color-base-300` линия, `--color-base-content` текст, `--color-primary` акцент…), где каждый цвет — `var(--tg-theme-…, #…)`; `--radius-box: 10px; --radius-field: 8px; --radius-selector: 6px`; `@media (prefers-color-scheme: dark)` переключает на `hub-dark`.
- `app.js` (без фреймворков, < 120 строк): если `Telegram.WebApp.initData` непуст — `POST /auth/tma`, токен в `sessionStorage`, тема по `colorScheme` и событию `themeChanged`; `htmx:configRequest` добавляет `Authorization: Bearer` при наличии токена; на событие `reauth` — повторный `/auth/tma` или переход на страницу входа; `BackButton` показывается, если у `<main>` есть `data-back`.
- `scripts/fetch_assets.sh VERSION_FILE`: качает в `.tools/` Tailwind standalone CLI 4.x (linux-x64 для Docker, плюс текущая ОС для разработки), `daisyui.mjs` и `daisyui-theme.mjs` 5.x, `htmx.min.js` 2.x — точные версии и sha256 в `scripts/assets.lock`; несовпадение хеша — ошибка. Точные версии и способ подключения daisyUI к standalone CLI — **сверить с официальной документацией daisyUI («Tailwind CSS Standalone») при реализации**, записать в отчёт.

- [ ] **Step 1: Падающие тесты** (`httpx.AsyncClient(transport=ASGITransport(app))`): `test_healthz_open`, `test_pages_require_login_redirects_to_login_hint`, `test_htmx_401_has_reauth_trigger`, `test_link_login_sets_secure_cookie_and_redirects`, `test_used_link_shows_expired_page`, `test_tma_login_returns_token`, `test_bearer_accepted_instead_of_cookie`, `test_post_without_htmx_header_or_bearer_is_403`, `test_security_headers_present` (CSP с `frame-ancestors` и `https://telegram.org`, nosniff, no-referrer), `test_logout_revokes`.
- [ ] **Step 2: Run** `uv run pytest tests/test_web_auth.py -q` → FAIL.
- [ ] **Step 3: Реализовать.** Шаблоны — по макетам ряда «Выбран A»; классы daisyUI (`btn`, `card`, `badge`, `menu`, `dock`, `tabs`) + утилиты Tailwind.
- [ ] **Step 4: Собрать CSS локально** `bash scripts/fetch_assets.sh && .tools/tailwindcss -i styles/app.css -o hub/web/static/app.css --minify` → файл есть, в нём `hub-light` и `hub-dark`.
- [ ] **Step 5: Run** весь набор → PASS.
- [ ] **Step 6: Commit** `feat: каркас веб-дашборда, вход, стиль A`

---

### Task 4: Ввод записи — общий для бота и веба

**Files:** Modify `hub/service.py`; Test `tests/test_service_intake.py`.

**Interfaces — Produces:**
- `async def Service.add_entry(self, user: User, text: str) -> tuple[Entry, str | None]` — вынесенное ядро `Service.text`: classify → `resolve_due` → раздел → `create_entry` (или заметка при `LlmError`/`BudgetExceeded`) → для проекта предложение подзадач в body; второе значение — пояснение для пользователя («Не разобрал, сохранил как заметку», «дневной лимит модели исчерпан», «Подзадачи предложить не удалось.») или `None`. `Service.text` вызывает его и строит `Reply` как раньше — **поведение бота не меняется** (все тесты фазы 1a остаются зелёными без правок ожиданий).
- `async def Service.proposal(self, user: User, entry_id: int) -> list[str] | None` — предложенные подзадачи проекта (для веба), только владельцу.

- [ ] **Step 1: Падающие тесты:** `test_add_entry_returns_entry_and_none_note`, `test_add_entry_llm_failure_note`, `test_proposal_owner_only`.
- [ ] **Step 2–4:** FAIL → реализовать → весь набор PASS.
- [ ] **Step 5: Commit** `refactor: общий ввод записи для бота и веба`

---

### Task 5: Экраны «Задачи» и действия с записями

**Files:** Create `hub/web/routes_pages.py` (часть «Задачи»), `hub/web/routes_actions.py`, `hub/web/templates/tasks.html`, `hub/web/templates/partials/task_row.html`, `hub/web/templates/partials/task_groups.html`, `tests/test_web_tasks.py`.

**Interfaces — Produces:**
- `GET /tasks?f=mine|shared|all&s=<section_id>` — группы «Просрочено / Сегодня / На неделе / Позже / Без срока / Подумать» (как в макете), фильтр: `mine` — `owner_id == user.id`, `shared` — чужие видимые или расшаренные (через `shared_ids`), `all` — все видимые; плашки разделов из `list_sections` с числом открытых задач.
- `POST /entries` (форма `text`) → `Service.add_entry` → ответ: обновлённые группы (`partials/task_groups.html`) + тост с пояснением, если есть.
- `POST /entries/{id}/done | /drop | /reopen | /kind/{kind} | /section/{section_id|0}` → `complete`/`drop`/`reopen`/`set_kind`/`set_section` → ответ `partials/task_row.html`; `PermissionError`/`LookupError` → 404 с «Нет доступа».
- `POST /entries/{id}/share` (форма `to=user:<id>|space:<id>`) → `access.share`; меню получателей — те же, что в боте.
- `POST /entries/{id}/subtasks` → создать предложенные подзадачи (та же логика, что `pj:ok`, атомарная заявка).
- Строка задачи: чекбокс «выполнено» (`btn` 44 px), название, мета (раздел, «общая» иконкой с `aria-label`), бейдж срока по шкале (просрочено — красный, ≤ 7 дн. — янтарный, дальше — серый; всегда с текстом), меню действий (`dropdown`): «Отменить», «В раздел…», «Поделиться…», «Тип…».

- [ ] **Step 1: Падающие тесты:** `test_tasks_page_groups_and_filters`, `test_add_entry_via_web_creates_task`, `test_done_drop_reopen_via_web`, `test_action_on_hidden_entry_404_no_leak` (тело ответа не содержит названия чужой задачи), `test_shared_filter_shows_partner_shared_task`, `test_create_subtasks_twice_no_duplicates`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5: Commit** `feat: экран «Задачи» в вебе`

---

### Task 6: Календари по ссылке iCal

**Files:** Create `hub/connections.py`, `hub/calendar_ics.py`, `tests/test_connections.py`, `tests/test_calendar_ics.py`, `tests/fixtures/calendar.ics`.

**Interfaces — Produces:**
- `connections.py`: `@dataclass Connection(id, owner_id, kind, name, hint, status, last_sync_at, last_success_at, last_error)`; `async def add_connection(db, box: SecretBox, owner: User, kind, name, secret: str, hint: str, clock) -> Connection`; `async def list_connections(db, user: User) -> list[Connection]` (только свои); `async def get_secret(db, box, user, connection_id) -> str` (только владелец, иначе `PermissionError`); `async def set_status(db, connection_id, status, error: str | None, clock, success: bool) -> None`; `async def disable_connection(db, user, connection_id, clock) -> None` (статус `disabled`, секрет затирается шифротекстом пустой строки — не удаление записи).
- `calendar_ics.py`:
  - `class UnsafeUrl(ValueError)`; `async def check_url(url: str, resolver=…) -> None` — Global Constraints (схема, частные адреса — `ipaddress.ip_address(x).is_private/is_loopback/is_link_local/is_reserved/is_multicast`; резолвер подменяется в тестах).
  - `async def fetch_ics(url: str, client: httpx.AsyncClient, resolver=…) -> bytes` — `follow_redirects=False`, свои ≤ 3 редиректа с `check_url` на каждом; потоковое чтение с обрывом на 2 МБ (`ValueError("файл календаря больше 2 МБ")`).
  - `@dataclass Event(uid, starts_at: datetime, ends_at: datetime | None, all_day: bool, title: str, location: str | None)`; `def parse_events(raw: bytes, day_from: date, day_to: date) -> list[Event]` — `recurring_ical_events.of(Calendar.from_ical(raw)).between(...)`, время → Europe/Moscow, событие без `DTSTART` пропускается, без названия → «(без названия)».
  - `async def sync_connection(db, box, conn: Connection, client, clock, resolver=…) -> None` — окно сегодня−1 … сегодня+7 дней; события окна заменяются в одной транзакции; любая ошибка → `set_status(error, текст)` без потери старых событий.
  - `async def events_for_day(db, user: User, day: date) -> list[Event]` — события из **своих** подключений со статусом не `disabled`, по времени; весь день — первыми.

- [ ] **Step 1: Падающие тесты:** `test_parse_simple_and_recurring_and_all_day` (фикстура: разовое, еженедельное `RRULE`, событие на весь день, событие в UTC → МСК), `test_event_without_dtstart_skipped`, `test_check_url_rejects_http_private_loopback_linklocal`, `test_redirect_to_metadata_ip_rejected` (`httpx.MockTransport`), `test_fetch_over_2mb_rejected`, `test_sync_error_keeps_old_events_and_sets_error`, `test_events_for_day_only_own_connections`, `test_get_secret_other_user_permission_error`, `test_secret_not_stored_plain`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5: Commit** `feat: календари по секретной ссылке iCal`

---

### Task 7: Экран «Настройки»

**Files:** Create `hub/web/templates/settings.html`, `hub/web/templates/partials/connection_row.html`, `tests/test_web_settings.py`; Modify `hub/web/routes_pages.py`, `hub/web/routes_actions.py`, `hub/users.py`.

**Interfaces — Produces:**
- `GET /settings` — блоки как в макете: профиль; «Подключения» (точка статуса цветом + словами: «Работает · синхр. N мин назад», «Ошибка: …», «Отключено»; кнопки «Проверить», «Отключить»); форма «Добавить календарь» (название + секретная ссылка, `type=url`, подсказка где взять в Google и Яндексе); «Брифинг» (время `HH:MM`); «Расход модели · сегодня» (свои `llm_usage`, для админа — по всем пользователям); «Доступ» (сколько я расшарил / мне); для админа — «Пользователи» и кнопка «Пригласить» (ссылка как в боте).
- `POST /connections` (форма `name`, `url`) → `check_url` → пробная загрузка и разбор → `add_connection` (`hint` = хост) + `sync_connection`; ошибка → форма с текстом ошибки, ничего не сохранено.
- `POST /connections/{id}/check` → `sync_connection` → `partials/connection_row.html`; `POST /connections/{id}/disable`.
- `POST /settings/brief-time` (форма `HH:MM`, 05:00–12:00) → `users.set_brief_time(db, user, value)` → `on_brief_time_changed(user)`.
- `users.py`: `async def set_brief_time(db, user: User, value: str) -> User` (валидация формата и диапазона → `ValueError`).

- [ ] **Step 1: Падающие тесты:** `test_settings_lists_only_own_connections`, `test_add_calendar_unsafe_url_rejected_nothing_saved`, `test_add_calendar_ok_saves_and_syncs` (подменённый загрузчик), `test_check_connection_other_user_404`, `test_brief_time_validation_and_reschedule_callback`, `test_admin_sees_users_and_invite_user_does_not`, `test_secret_never_rendered` (в HTML нет ссылки целиком, только хост).
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5: Commit** `feat: экран «Настройки» и подключение календарей`

---

### Task 8: Экран «Сегодня», календарь в брифинге, синхронизация по расписанию

**Files:** Create `hub/web/templates/today.html`, `hub/web/templates/partials/today_cards.html`, `tests/test_web_today.py`; Modify `hub/brief.py`, `hub/scheduler.py`, `hub/web/routes_pages.py`; Test `tests/test_brief.py`, `tests/test_scheduler.py`.

**Interfaces — Produces:**
- `BriefFacts.events: list[Event]` (новое поле, по умолчанию пусто); `collect(db, user, day)` заполняет из `events_for_day`; `render_lists` — первым разделом «Календарь» строки `HH:MM Название` (весь день — «весь день · Название»), остальное как было (§10: календарь → горит → сроки → …).
- `GET /` — «Сегодня»: шапка («Доброе утро, <имя>», дата, строка статуса «N просрочено · M на сегодня»), карточки «Требует внимания», «Сегодня» (события и задачи одной лентой по времени), «Скоро истекает» (пустая карточка-заглушка «появится с документами» до 1b), «Почта» (заглушка до фазы 2); от 1024 px — две колонки. Каждая карточка — `hx-get="/partials/today/<card>" hx-trigger="load, every 10m"`.
- `GET /mail`, `GET /docs` — `stub.html` («Появится позже»), вкладки в навигации есть.
- `scheduler.py`: `def schedule_calendar_sync(scheduler, db, box, client_factory, clock) -> None` — задача `calendar_sync` раз в 15 минут (`IntervalTrigger`, `coalesce`, `misfire_grace_time=600`), синхронизирует все подключения `ics_calendar` не в статусе `disabled`, по одному; ошибки не роняют задачу.

- [ ] **Step 1: Падающие тесты:** `test_brief_has_calendar_first_with_times`, `test_brief_without_calendar_unchanged` (старые тесты брифинга остаются зелёными), `test_today_page_cards_and_two_columns_class`, `test_today_shows_only_own_calendar`, `test_calendar_sync_job_registered_and_survives_error`, `test_stub_pages`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5: Commit** `feat: экран «Сегодня» и календарь в брифинге`

---

### Task 9: Бот и запуск — `/web`, кнопка мини-приложения, веб-сервер в процессе

**Files:** Modify `hub/service.py`, `hub/bot.py`, `hub/__main__.py`; Test `tests/test_service_commands.py`, `tests/test_bot.py`.

**Interfaces — Produces:**
- `async def Service.web_link(self, tg_id: int) -> Reply | None` — незнакомцу `None`; иначе `Reply("Ссылка для входа на 10 минут:\n<web_base_url>/auth/link?t=<token>")`.
- `Button.web_app: str | None = None`; `reply_markup` строит `InlineKeyboardButton(web_app=WebAppInfo(url=...))`, когда поле задано. `/settings` → `Reply` с кнопкой «⚙️ Открыть дашборд» (`web_app` = `<web_base_url>/settings`); `/help` дополнен строками `/web` и `/settings`.
- `__main__.py`: `uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=settings.web_port, proxy_headers=True, forwarded_allow_ips="*", log_config=None, access_log=False))`; `await asyncio.gather(dp.start_polling(bot), server.serve())`; при остановке одного — останавливается второе, ресурсы закрываются как сейчас. `on_brief_time_changed` = `schedule_user(...)`. `schedule_calendar_sync(...)` при старте. Логи доступа не пишутся (в URL ссылки входа — токен).

- [ ] **Step 1: Падающие тесты:** `test_web_link_for_registered_only`, `test_settings_command_has_web_app_button`, `test_reply_markup_web_app_button`, `test_help_lists_web_and_settings`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS; `uv run python -c "import hub.__main__"` без ошибок.
- [ ] **Step 5: Commit** `feat: /web, кнопка мини-приложения, веб в одном процессе`

---

### Task 10: Сборка, деплой, nginx и домен

**Files:** Modify `Dockerfile`, `compose.yml`, `.env.example`, `ops/DEPLOY.md`.

**Interfaces:**
- `Dockerfile`: первая стадия (`debian:bookworm-slim` или `alpine` с `bash`, `curl`, `sha256sum`) запускает `scripts/fetch_assets.sh` (linux-x64) и собирает `app.css`; финальный образ копирует `hub/web/static/` (с собранными `app.css` и `htmx.min.js`); `EXPOSE 8080`.
- `compose.yml`: `mem_limit: 384m`; `expose: ["8080"]`; сеть общего reverse proxy (`external: true`) плюс default; портов наружу нет.
- `ops/DEPLOY.md`: шаги ниже; `.env` дополняется `CONNECTIONS_KEY` (генерируется на сервере, не выводится), `WEB_BASE_URL=https://desk.example.com`.

- [ ] **Step 1: Сборка образа на сервере** `docker compose build` → успех; `docker run --rm personal-hub:latest python -c "import hub.web"`; `ls hub/web/static/app.css` в образе.
- [ ] **Step 2: Владелец (руками или по его слову):** A-запись `desk.example.com` → публичный IP VPS (у кого управляется DNS `example.com` — уточнить у владельца).
- [ ] **Step 3 (только по прямому «да» владельца — общий reverse proxy (nginx)):** серверный блок `desk.conf` в конфиге общего nginx по образцу существующих: `listen 80` с `/.well-known/acme-challenge/` и редиректом на https; `listen 443 ssl` для `desk.example.com` с allowlist `<VPN_SUBNET>; deny all;`, `proxy_pass` через переменную-резолвер на `personal-hub-hub-1:8080`, `proxy_set_header X-Forwarded-Proto $scheme; Host; X-Forwarded-For`; `client_max_body_size 1m`. Сертификат — выпуск через ACME (webroot-challenge) штатным способом хоста. Проверка `nginx -t`, затем `nginx -s reload` (nginx кэширует IP апстримов).
- [ ] **Step 4: Запуск** `docker compose up -d --build` → `docker compose ps` running; `curl -s https://desk.example.com/healthz` **изнутри VPN** → `{"ok":true}`; снаружи VPN → 403; память < 384 МБ.
- [ ] **Step 5: Дымовая проверка с владельцем:** `/web` → ссылка → вход в браузере; `/settings` → кнопка открывает дашборд в Telegram (при включённом VPN на телефоне); добавить календарь по секретной ссылке → «Работает»; событие сегодня видно на «Сегодня» и в `/brief`; задача из веба видна в боте и наоборот; партнёр не видит личных задач владельца.
- [ ] **Step 6: Commit** `ops: веб-дашборд в Docker, nginx и домен`

---

## Что в 1c сознательно не делаем

Почта (фаза 2), документы и облако (1b), MCP (фаза 5), Notion (фаза 2), семейный чат и голос (1d), вход через Telegram OpenID Connect (только если ссылка надоест), push-уведомления, офлайн-режим. Экраны «Почта» и «Документы» — заглушки.

## Критерий готовности 1c

Владелец и партнёр входят в дашборд с ПК (ссылка `/web`) и из Telegram (кнопка `/settings`) через VPN; «Задачи» в вебе и в боте показывают одно и то же; календарь Google или Яндекс подключён по секретной ссылке, его события на «Сегодня» и первым разделом в утреннем брифинге; из-вне VPN `desk.example.com` отвечает 403.
