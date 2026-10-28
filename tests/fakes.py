"""Заглушки для тестов: без сети и без настоящей модели."""

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import Any

from hub.llm import Classification, Llm, LlmError


class FakeLlm(Llm):
    """Отдаёт заготовленные ответы по порядку; все вызовы пишет в calls."""

    def __init__(
        self,
        classifications: list[Classification] | None = None,
        subtasks: list[list[str]] | None = None,
        intros: list[str] | None = None,
        fail: bool = False,
    ) -> None:
        self.classifications = list(classifications or [])
        self.subtasks = list(subtasks or [])
        self.intros = list(intros or [])
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def classify(
        self, user_id: int, text: str, today: date, sections: list[str]
    ) -> Classification:
        self.calls.append(
            {
                "method": "classify",
                "user_id": user_id,
                "text": text,
                "today": today,
                "sections": sections,
            }
        )
        if self.fail:
            raise LlmError("fake failure")
        return self.classifications.pop(0)

    async def decompose(self, user_id: int, title: str, body: str) -> list[str]:
        self.calls.append({"method": "decompose", "user_id": user_id, "title": title, "body": body})
        if self.fail:
            raise LlmError("fake failure")
        return self.subtasks.pop(0)

    async def brief_intro(self, user_id: int, facts_text: str) -> str:
        self.calls.append({"method": "brief_intro", "user_id": user_id, "facts_text": facts_text})
        if self.fail:
            raise LlmError("fake failure")
        return self.intros.pop(0)


@dataclass
class _Msg:
    content: list[Any]
    usage: Any
    stop_reason: str = "end_turn"


class _Messages:
    def __init__(self, owner: "RecordingClient") -> None:
        self._owner = owner

    async def create(self, **kwargs: Any) -> _Msg:
        self._owner.kwargs = kwargs
        return _Msg(
            self._owner.content,
            SimpleNamespace(input_tokens=10, output_tokens=5),
            self._owner.stop_reason,
        )


class RecordingClient:
    """Заглушка AsyncAnthropic: запоминает kwargs последнего messages.create."""

    def __init__(
        self,
        tool_input: dict[str, Any] | None = None,
        text: str | None = None,
        thinking: str | None = None,
        stop_reason: str = "end_turn",
    ) -> None:
        self.kwargs: dict[str, Any] = {}
        self.stop_reason = stop_reason
        if text is not None:
            self.content: list[Any] = [SimpleNamespace(type="text", text=text)]
            if thinking is not None:
                self.content.insert(0, SimpleNamespace(type="thinking", thinking=thinking))
        elif tool_input is not None:
            self.content = [SimpleNamespace(type="tool_use", name="tool", input=tool_input)]
        else:
            self.content = []
        self.messages = _Messages(self)
