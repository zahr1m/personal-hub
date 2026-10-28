"""Обёртка над Anthropic API: маскирование, бюджет, метка расхода."""

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

import aiosqlite
import anthropic

from hub.budget import check_budget, record_usage
from hub.clock import Clock
from hub.config import Settings
from hub.entries import CONTOURS
from hub.models import EntryKind
from hub.pii import mask_pii

KINDS: tuple[EntryKind, ...] = ("task", "project", "think", "note")
TITLE_MAX = 120
WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

CLASSIFY_PROMPT = (
    "Ты разбираешь короткую запись из личного Telegram-бота пары. "
    "Сегодня {today}, {weekday}. Определи тип записи: task — одно конкретное действие "
    "(купить билеты, позвонить в УК, оплатить счёт); project — дело из нескольких шагов, "
    "которое нельзя сделать за один раз (переоформить машину, подготовить праздник, "
    "организовать переезд, сделать ремонт, оформить визу); think — мысль для обдумывания; "
    "note — заметка. Сформулируй заголовок до 120 символов. "
    "Срок (due_date) укажи только если он назван явно, считай от сегодняшней даты. "
    "Контуры: {contours}. Разделы пользователя: {sections}. "
    "Если контур или раздел не очевиден — оставь поле null, не угадывай. "
    "Ответь вызовом инструмента save_entry."
)
DECOMPOSE_PROMPT = (
    "Разбей проект на 3–7 конкретных шагов-подзадач. Каждый шаг — короткая фраза "
    "в повелительном наклонении, без нумерации."
)
BRIEF_PROMPT = (
    "Ты пишешь вступление к утреннему брифу для пары. По фактам ниже напиши 2–3 "
    "тёплых предложения обычным текстом: без списков, без markdown, без выдуманных фактов."
)

SAVE_ENTRY_TOOL: dict[str, Any] = {
    "name": "save_entry",
    "description": "Сохранить разобранную запись",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "title": {"type": "string", "maxLength": TITLE_MAX},
            "due_date": {"type": ["string", "null"], "description": "YYYY-MM-DD или null"},
            "contour": {"type": ["string", "null"], "enum": [*CONTOURS, None]},
            "section": {"type": ["string", "null"]},
        },
        "required": ["kind", "title", "due_date", "contour", "section"],
    },
}
SUBTASKS_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"subtasks": {"type": "array", "items": {"type": "string"}}},
        "required": ["subtasks"],
        "additionalProperties": False,
    },
}


class LlmError(Exception):
    """Сбой обращения к модели или невалидный ответ."""


@dataclass
class Classification:
    kind: EntryKind
    title: str
    due_date: date | None
    contour: str | None
    section: str | None


class Llm(Protocol):
    async def classify(
        self, user_id: int, text: str, today: date, sections: list[str]
    ) -> Classification: ...

    async def decompose(self, user_id: int, title: str, body: str) -> list[str]: ...

    async def brief_intro(self, user_id: int, facts_text: str) -> str: ...


class AnthropicLlm(Llm):
    def __init__(
        self,
        settings: Settings,
        db: aiosqlite.Connection,
        clock: Clock,
        client: Any = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._clock = clock
        self._owns_client = client is None
        self._client = client or anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key, timeout=60, max_retries=1
        )

    async def aclose(self) -> None:
        """Закрывает клиент, только если он создан здесь."""
        if self._owns_client:
            await self._client.close()

    async def _call(
        self,
        user_id: int,
        model: str,
        max_tokens: int,
        system: str,
        user_text: str,
        tool: dict[str, Any] | None = None,
        output_config: dict[str, Any] | None = None,
    ) -> Any:
        """Бюджет → маскирование → запрос → учёт расхода (только если ответ получен)."""
        day = self._clock.today()
        await check_budget(self._db, user_id, day, self._settings)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": mask_pii(user_text)}],
            "metadata": {"user_id": f"personal-hub:{user_id}"},
        }
        if tool:
            kwargs["tools"] = [tool]
            kwargs["tool_choice"] = {"type": "tool", "name": tool["name"]}
        if output_config:
            kwargs["output_config"] = output_config
        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.APIError as e:
            raise LlmError(f"сбой Anthropic API: {e}") from e
        await record_usage(
            self._db, user_id, day, response.usage.input_tokens, response.usage.output_tokens
        )
        return response

    @staticmethod
    def _text(response: Any) -> str:
        """Склеивает text-блоки (thinking игнорируется)."""
        return "".join(b.text for b in response.content if b.type == "text").strip()

    @staticmethod
    def _tool_input(response: Any) -> dict[str, Any]:
        for block in response.content:
            if block.type == "tool_use" and isinstance(block.input, dict):
                return block.input
        raise LlmError("в ответе нет вызова инструмента")

    async def classify(
        self, user_id: int, text: str, today: date, sections: list[str]
    ) -> Classification:
        system = CLASSIFY_PROMPT.format(
            today=today.isoformat(),
            weekday=WEEKDAYS[today.weekday()],
            contours=", ".join(CONTOURS),
            sections=", ".join(sections) if sections else "нет",
        )
        response = await self._call(
            user_id, self._settings.model_fast, 512, system, text, SAVE_ENTRY_TOOL
        )
        data = self._tool_input(response)
        kind = data.get("kind")
        if kind not in KINDS:
            raise LlmError(f"неизвестный kind: {kind!r}")
        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            raise LlmError("пустой заголовок")
        due = data.get("due_date")
        due_date: date | None = None
        if due is not None:
            try:
                due_date = date.fromisoformat(due)
            except (TypeError, ValueError) as e:
                raise LlmError(f"некорректная дата: {due!r}") from e
        contour = data.get("contour")
        section = data.get("section")
        return Classification(
            kind,
            title.strip()[:TITLE_MAX],
            due_date,
            contour if contour in CONTOURS else None,
            section if section in sections else None,
        )

    async def decompose(self, user_id: int, title: str, body: str) -> list[str]:
        response = await self._call(
            user_id,
            self._settings.model_smart,
            2048,
            DECOMPOSE_PROMPT,
            f"{title}\n\n{body}",
            output_config={"effort": "low", "format": SUBTASKS_FORMAT},
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise LlmError(f"подзадачи: ответ не получен ({response.stop_reason})")
        try:
            subtasks = json.loads(self._text(response)).get("subtasks")
        except (ValueError, AttributeError) as e:
            raise LlmError("подзадачи: невалидный JSON") from e
        if (
            not isinstance(subtasks, list)
            or not 3 <= len(subtasks) <= 7
            or not all(isinstance(s, str) and s.strip() for s in subtasks)
        ):
            raise LlmError("подзадачи: нужен список из 3–7 строк")
        return [s.strip() for s in subtasks]

    async def brief_intro(self, user_id: int, facts_text: str) -> str:
        response = await self._call(
            user_id,
            self._settings.model_smart,
            1024,
            BRIEF_PROMPT,
            facts_text,
            output_config={"effort": "low"},
        )
        text = self._text(response)
        if not text:
            raise LlmError("пустое вступление")
        return text
