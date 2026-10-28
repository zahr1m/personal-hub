"""Мозг бота без Telegram: tg_id + текст/команда/колбэк → Reply (None — молчим)."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, get_args

import aiosqlite

from hub.access import can_see_entry, share, visible_entries_sql
from hub.auth import create_login_link
from hub.brief import build as build_brief
from hub.brief import build_full, save_manual_intro
from hub.budget import BudgetExceeded
from hub.clock import Clock
from hub.config import Settings
from hub.entries import (
    Entry,
    complete,
    create_entry,
    drop,
    get_entry,
    list_open_tasks,
    reopen,
    resolve_due,
    set_kind,
    set_section,
    shared_ids,
)
from hub.llm import TITLE_MAX, Classification, Llm, LlmError
from hub.models import EntryKind, User
from hub.sections import Section, get_or_create_section, list_sections
from hub.users import (
    create_invite,
    family_space_id,
    get_user_by_tg,
    list_users,
    redeem_invite,
)

log = logging.getLogger(__name__)

KIND_LABELS: dict[str, str] = {
    "task": "Задача",
    "project": "Проект",
    "think": "Подумать",
    "note": "Заметка",
}
PROPOSAL_HEADER = "Подзадачи (предложение):"
NO_ACCESS = "Нет доступа"
NOT_PARSED = "Не разобрал, сохранил как заметку"
BUDGET_NOTE = "дневной лимит модели исчерпан"
DECOMPOSE_FAILED = "Подзадачи предложить не удалось."
TASKS_LIMIT = 20
TASKS_PER_ROW = 4
SECTION_WINDOW = timedelta(minutes=10)
SECTION_USAGE = "/section <имя раздела>"
HELP = (
    "/tasks — открытые задачи\n"
    "/sections — разделы и число задач\n"
    "/section <имя> — раздел для последней записи\n"
    "/brief — брифинг сейчас\n"
    "/web — ссылка для входа в веб-панель\n"
    "/settings — открыть дашборд мини-приложением\n"
    "/invite — ссылка-приглашение (админ)\n"
    "/help — эта справка\n"
    "Всё остальное — записи текстом, я их разберу."
)
ADMIN_NOTICE = "Администратор сервера технически может видеть данные на нём."


@dataclass
class Button:
    text: str
    data: str  # ≤ 64 байт — лимит Telegram
    style: Literal["success", "danger", "primary"] | None = None
    web_app: str | None = None  # url мини-приложения вместо callback


@dataclass
class Reply:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)


def _split_proposal(body: str) -> tuple[str, list[str] | None]:
    """Тело без хвостового блока предложения и сам список (None — хвостового блока нет)."""
    head, sep, tail = body.rpartition(PROPOSAL_HEADER)
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if not sep or not head.endswith("\n\n") or not lines:
        return body, None
    if not all(ln.startswith("- ") for ln in lines):
        return body, None
    return head.rstrip(), [ln[2:].strip() for ln in lines if ln[2:].strip()]


class Service:
    def __init__(
        self,
        db: aiosqlite.Connection,
        llm: Llm,
        clock: Clock,
        settings: Settings,
        on_register: Callable[[User], Awaitable[None]] | None = None,
    ) -> None:
        self._on_register = on_register
        self._db = db
        self._llm = llm
        self._clock = clock
        self._settings = settings

    async def start(self, tg_id: int, name: str, payload: str | None) -> Reply | None:
        user = await get_user_by_tg(self._db, tg_id)
        if user is not None:
            return Reply(f"Привет, {user.name}! Пиши записи текстом — я их разберу.")
        if not payload:
            return None
        allowed = self._settings.allowed_tg_ids
        if allowed and tg_id not in (*allowed, self._settings.admin_tg_id):
            return None  # молчим, код не тратим
        user = await redeem_invite(
            self._db, payload, tg_id, name, self._clock, self._settings.user_names
        )
        if user is None:
            return None
        if self._on_register:
            try:
                await self._on_register(user)
            except Exception:  # пользователь уже записан — приветствие всё равно уходит
                log.exception("on_register failed")
        return Reply(
            f"Привет, {user.name}! Ты в списке. Пиши записи текстом — я их разберу.\n{ADMIN_NOTICE}"
        )

    async def help(self, tg_id: int) -> Reply | None:
        if await get_user_by_tg(self._db, tg_id) is None:
            return None
        return Reply(HELP)

    async def web_link(self, tg_id: int) -> Reply | None:
        user = await get_user_by_tg(self._db, tg_id)
        if user is None:
            return None
        token = await create_login_link(self._db, user, self._clock)
        base = self._settings.web_base_url.rstrip("/")
        return Reply(f"Ссылка для входа на 10 минут:\n{base}/auth/link?t={token}")

    async def settings_button(self, tg_id: int) -> Reply | None:
        if await get_user_by_tg(self._db, tg_id) is None:
            return None
        url = f"{self._settings.web_base_url.rstrip('/')}/settings"
        return Reply("Настройки и дашборд:", [[Button("⚙️ Настройки", "", web_app=url)]])

    async def invite(self, tg_id: int, bot_username: str) -> Reply | None:
        user = await get_user_by_tg(self._db, tg_id)
        if user is None or user.role != "admin":
            return None
        code = await create_invite(self._db, user, self._clock)
        return Reply(f"https://t.me/{bot_username}?start={code}\nСсылка действует 24 часа.")

    async def text(self, tg_id: int, text: str) -> Reply | None:
        user = await get_user_by_tg(self._db, tg_id)
        if user is None or not text.strip():
            return None
        entry, note = await self.add_entry(user, text)
        if note is not None and note.startswith(NOT_PARSED):
            return Reply(note, self._buttons(entry))
        reply = Reply(await self._line(entry), self._buttons(entry))
        if note is not None:
            reply.text += f"\n{note}"
        items = _split_proposal(entry.body)[1] if entry.kind == "project" else None
        if items is not None:
            self._render_proposal(entry, items, reply)
        return reply

    async def add_entry(self, user: User, text: str) -> tuple[Entry, str | None]:
        """Ядро ввода записи для бота и веба: запись и пояснение пользователю (или None)."""
        sections = await list_sections(self._db, user)
        try:
            c = await self._llm.classify(
                user.id, text, self._clock.today(), [s.name for s in sections]
            )
        except (LlmError, BudgetExceeded) as e:
            note = NOT_PARSED
            if isinstance(e, BudgetExceeded):
                note += "\n" + BUDGET_NOTE
            entry = await create_entry(
                self._db, user, "note", text.strip()[:TITLE_MAX], text, clock=self._clock
            )
            return entry, note
        entry = await create_entry(
            self._db,
            user,
            c.kind,
            c.title,
            text,
            resolve_due(c.due_date, self._clock.today()),
            c.contour,
            self._pick_section(c, sections, user.id),
            clock=self._clock,
        )
        if entry.kind == "project" and await self._write_proposal(user, entry) is None:
            return entry, DECOMPOSE_FAILED
        return entry, None

    async def proposal(self, user: User, entry_id: int) -> list[str] | None:
        """Предложенные подзадачи проекта; только владельцу."""
        row = await (
            await self._db.execute(
                "SELECT owner_id, kind, body FROM entries WHERE id = ?", (entry_id,)
            )
        ).fetchone()
        if row is None or row["owner_id"] != user.id or row["kind"] != "project":
            return None
        return _split_proposal(row["body"])[1]

    async def create_subtasks(self, user: User, entry_id: int) -> str:
        """Создаёт предложенные подзадачи (как кнопка pj:ok); текст — итог для пользователя."""
        if not await can_see_entry(self._db, user.id, entry_id):
            raise PermissionError("запись недоступна")
        return (await self._project_choice(user, entry_id, True)).text

    @staticmethod
    def _pick_section(c: Classification, sections: list[Section], user_id: int) -> int | None:
        """Только раздел из видимых пользователю (свой раньше чужого), иначе без раздела."""
        if c.section is None:
            return None
        wanted = c.section.strip().casefold()
        named = [s for s in sections if s.name.strip().casefold() == wanted]
        named.sort(key=lambda s: s.owner_id != user_id)
        return named[0].id if named else None

    async def _write_proposal(self, user: User, entry: Entry) -> list[str] | None:
        """Предложение подзадач в хвост body (и в entry); None — модель не справилась."""
        try:
            subtasks = await self._llm.decompose(user.id, entry.title, entry.body)
        except (LlmError, BudgetExceeded):
            return None
        subtasks = [" ".join(s.split()) for s in subtasks if s.strip()]
        block = PROPOSAL_HEADER + "\n" + "\n".join(f"- {s}" for s in subtasks)
        entry.body = f"{entry.body}\n\n{block}"
        await self._set_body(entry.id, entry.body)
        return subtasks

    @staticmethod
    def _render_proposal(entry: Entry, subtasks: list[str], reply: Reply) -> None:
        numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(subtasks, 1))
        reply.text += f"\n\nПредлагаю подзадачи:\n{numbered}"
        reply.buttons.append(
            [
                Button("✅ Создать подзадачи", f"pj:{entry.id}:ok"),
                Button("Не нужно", f"pj:{entry.id}:no"),
            ]
        )

    async def _switch_kind(
        self, user: User, entry_id: int, kind: EntryKind
    ) -> tuple[Entry, list[str] | None, str | None]:
        """Меняет тип (владелец). Переход в «проект» предлагает подзадачи, если их ещё нет.

        Возвращает запись, новое предложение (None — не предлагали) и пояснение.
        """
        before = await get_entry(self._db, user, entry_id)
        entry = await set_kind(self._db, user, entry_id, kind)
        if kind != "project" or before.kind == "project" or _split_proposal(entry.body)[1]:
            return entry, None, None
        proposed = await self._write_proposal(user, entry)
        return entry, proposed, None if proposed is not None else DECOMPOSE_FAILED

    async def set_kind(
        self, user: User, entry_id: int, kind: EntryKind
    ) -> tuple[Entry, str | None]:
        """Смена типа для бота и веба: запись и пояснение пользователю (или None)."""
        entry, _, note = await self._switch_kind(user, entry_id, kind)
        return entry, note

    async def describe(self, entry: Entry) -> str:
        """Строка-подтверждение записи, как в боте."""
        return await self._line(entry)

    async def _set_body(self, entry_id: int, body: str) -> None:
        await self._db.execute("UPDATE entries SET body = ? WHERE id = ?", (body, entry_id))
        await self._db.commit()

    async def _line(self, entry: Entry) -> str:
        parts = [f"📌 {KIND_LABELS[entry.kind]}: {entry.title}"]
        if entry.due_date:
            parts.append(f"до {entry.due_date:%d.%m}")
        if entry.section_id is not None:
            row = await (
                await self._db.execute(
                    "SELECT name FROM sections WHERE id = ?", (entry.section_id,)
                )
            ).fetchone()
            parts.append(f"раздел {row['name']}")
        return " · ".join(parts)

    @staticmethod
    def _reopen_button(entry: Entry) -> Button:
        return Button("↩️ Вернуть в работу", f"r:{entry.id}", "primary")

    @staticmethod
    def _buttons(entry: Entry) -> list[list[Button]]:
        rows = [
            [Button(label, f"k:{entry.id}:{kind}") for kind, label in KIND_LABELS.items()],
            [
                Button("Раздел…", f"s:{entry.id}"),
                Button("Поделиться", f"sh:{entry.id}"),
            ],
        ]
        if entry.kind == "task":
            rows[1].append(Button("✅ Выполнено", f"d:{entry.id}", "success"))
            rows[1].append(Button("❌ Отменить", f"x:{entry.id}", "danger"))
        return rows

    async def tasks(self, tg_id: int) -> Reply | None:
        user = await get_user_by_tg(self._db, tg_id)
        if user is None:
            return None
        today = self._clock.today()
        found = await list_open_tasks(self._db, user, today)
        if not found:
            return Reply("Открытых задач нет")
        shown = found[:TASKS_LIMIT]
        marked = await shared_ids(self._db, user, shown)
        lines = []
        for i, e in enumerate(shown, 1):
            line = f"{'⚠️' if e.due_date and e.due_date < today else '•'} {i}. {e.title}"
            if e.due_date:
                line += f" · {e.due_date:%d.%m}"
            if e.id in marked:
                line += " 👥"
            lines.append(line)
        if len(found) > len(shown):
            lines.append(f"и ещё {len(found) - len(shown)}")
        buttons = [Button(f"✔ {i}", f"d:{e.id}", "success") for i, e in enumerate(shown, 1)]
        rows = [buttons[i : i + TASKS_PER_ROW] for i in range(0, len(buttons), TASKS_PER_ROW)]
        return Reply("\n".join(lines), rows)

    async def brief(self, tg_id: int) -> Reply | None:
        """/brief вне расписания: briefs не трогает."""
        user = await get_user_by_tg(self._db, tg_id)
        if user is None:
            return None
        text, _ = await build_brief(self._db, self._llm, user, self._clock.today())
        return Reply(text)

    async def brief_intro(self, user: User) -> str | None:
        """Только вступление (для виджета): пишет brief_manual, briefs не трогает."""
        day = self._clock.today()
        _, _, intro = await build_full(self._db, self._llm, user, day)
        if intro:
            await save_manual_intro(self._db, user.id, day, intro, self._clock.now().isoformat())
        return intro

    async def sections(self, tg_id: int) -> Reply | None:
        user = await get_user_by_tg(self._db, tg_id)
        if user is None:
            return None
        found = await list_sections(self._db, user)
        if not found:
            return Reply(f"Разделов пока нет. Создать: {SECTION_USAGE}")
        cond, params = visible_entries_sql(user.id)
        sql = (
            "SELECT e.section_id, COUNT(*) AS n FROM entries AS e"  # noqa: S608
            f" WHERE {cond} AND e.kind = 'task' AND e.status = 'open'"
            " AND e.section_id IS NOT NULL GROUP BY e.section_id"
        )
        counts = {
            r["section_id"]: r["n"] for r in await (await self._db.execute(sql, params)).fetchall()
        }
        return Reply("\n".join(f"• {s.name} — {counts.get(s.id, 0)}" for s in found))

    async def section_command(self, tg_id: int, name: str) -> Reply | None:
        user = await get_user_by_tg(self._db, tg_id)
        if user is None:
            return None
        if not name.strip():
            return Reply(f"Укажи имя раздела: {SECTION_USAGE}")
        section = await get_or_create_section(self._db, user, name)
        row = await (
            await self._db.execute(
                "SELECT id, created_at FROM entries WHERE owner_id = ?"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (user.id,),
            )
        ).fetchone()
        age = self._clock.now() - datetime.fromisoformat(row["created_at"]) if row else None
        if age is None or age > SECTION_WINDOW:
            return Reply(f"Раздел «{section.name}» готов")
        entry = await set_section(self._db, user, row["id"], section.id)
        return Reply(await self._line(entry), self._buttons(entry))

    async def callback(self, tg_id: int, data: str) -> Reply | None:
        user = await get_user_by_tg(self._db, tg_id)
        parsed = self._parse(data)
        if user is None or parsed is None:
            return None
        action, entry_id, arg = parsed
        if not await can_see_entry(self._db, user.id, entry_id):
            return Reply(NO_ACCESS)
        try:
            return await self._dispatch(user, action, entry_id, arg)
        except PermissionError:
            return Reply(NO_ACCESS)

    @staticmethod
    def _parse(data: str) -> tuple[str, int, str] | None:
        parts = data.split(":")
        if parts[0] not in ("k", "s", "sh", "d", "x", "r", "pj", "shu", "shs", "ss"):
            return None
        want = 3 if parts[0] in ("k", "pj", "shu", "shs", "ss") else 2
        if len(parts) != want or not parts[1].isdecimal():
            return None
        arg = parts[2] if want == 3 else ""
        allowed = {"k": get_args(EntryKind), "pj": ("ok", "no")}.get(parts[0])
        if allowed is not None and arg not in allowed:
            return None
        if parts[0] in ("shu", "shs", "ss") and not arg.isdecimal():
            return None
        return parts[0], int(parts[1]), arg

    async def _dispatch(self, user: User, action: str, entry_id: int, arg: str) -> Reply:
        if action == "k":
            entry, proposed, note = await self._switch_kind(user, entry_id, arg)  # type: ignore[arg-type]
            reply = Reply(await self._line(entry), self._buttons(entry))
            if proposed is not None:
                self._render_proposal(entry, proposed, reply)
            elif note is not None:
                reply.text += "\n" + note
            return reply
        if action == "d":
            row = await (
                await self._db.execute("SELECT status FROM entries WHERE id = ?", (entry_id,))
            ).fetchone()
            if row["status"] == "done":
                return Reply("Уже сделано")
            if row["status"] == "dropped":
                return Reply("Задача отменена")
            entry = await complete(self._db, user, entry_id, self._clock)
            return Reply(f"✔ Готово: {entry.title}", [[self._reopen_button(entry)]])
        if action == "x":
            entry = await drop(self._db, user, entry_id, self._clock)
            if entry.status == "done":
                return Reply("Уже сделано")
            return Reply(f"{await self._line(entry)}\nОтменено", [[self._reopen_button(entry)]])
        if action == "r":
            entry = await reopen(self._db, user, entry_id)
            return Reply(await self._line(entry), self._buttons(entry))
        if action == "pj":
            return await self._project_choice(user, entry_id, arg == "ok")
        if not await self._is_owner(user, entry_id):
            return Reply(NO_ACCESS)
        if action == "sh":
            return await self._share_choices(user, entry_id)
        if action == "s":
            return await self._section_choices(user, entry_id)
        if action == "ss":
            entry = await set_section(self._db, user, entry_id, int(arg) or None)
            return Reply(await self._line(entry), self._buttons(entry))
        return await self._share(user, entry_id, action == "shs", int(arg))

    async def _is_owner(self, user: User, entry_id: int) -> bool:
        row = await (
            await self._db.execute("SELECT owner_id FROM entries WHERE id = ?", (entry_id,))
        ).fetchone()
        return row["owner_id"] == user.id

    async def _share_choices(self, user: User, entry_id: int) -> Reply:
        rows = [
            [Button(u.name, f"shu:{entry_id}:{u.id}")]
            for u in await list_users(self._db)
            if u.id != user.id
        ]
        space = await family_space_id(self._db)
        rows.append([Button("Семья", f"shs:{entry_id}:{space}")])
        return Reply("Кому открыть запись?", rows)

    async def _section_choices(self, user: User, entry_id: int) -> Reply:
        own = [s for s in await list_sections(self._db, user) if s.owner_id == user.id]
        rows = [[Button(s.name, f"ss:{entry_id}:{s.id}")] for s in own]
        rows.append([Button("Без раздела", f"ss:{entry_id}:0")])
        return Reply("В какой раздел?", rows)

    async def _share(self, user: User, entry_id: int, to_space: bool, principal_id: int) -> Reply:
        table = "spaces" if to_space else "users"
        sql = f"SELECT name FROM {table} WHERE id = ?"  # noqa: S608
        row = await (await self._db.execute(sql, (principal_id,))).fetchone()
        if row is None:
            return Reply("Не найдено")
        principal = "space" if to_space else "user"
        await share(self._db, user, "entry", entry_id, principal, principal_id, self._clock)
        return Reply(f"Поделился: {row['name']}")

    async def _project_choice(self, user: User, project_id: int, create: bool) -> Reply:
        row = await (
            await self._db.execute(
                "SELECT owner_id, kind, body, section_id FROM entries WHERE id = ?", (project_id,)
            )
        ).fetchone()
        if row["owner_id"] != user.id:
            return Reply(NO_ACCESS)
        if row["kind"] != "project":
            return Reply("Нечего создавать")
        body, items = _split_proposal(row["body"])
        if items is None:
            return Reply("Предложение уже обработано.")
        # апдейты идут параллельно: предложение забирает ровно один, проигравший не дублирует
        claim = await self._db.execute(
            "UPDATE entries SET body = ? WHERE id = ? AND body = ?",
            (body, project_id, row["body"]),
        )
        await self._db.commit()
        if claim.rowcount != 1:
            return Reply("Уже создано.")
        if not create:
            return Reply("Ок, без подзадач.")
        for title in items:
            await create_entry(
                self._db,
                user,
                "task",
                title,
                section_id=row["section_id"],
                parent_id=project_id,
                clock=self._clock,
            )
        return Reply(f"Создал подзадач: {len(items)}.")
