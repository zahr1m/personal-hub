"""Доменные типы."""

from dataclasses import dataclass
from typing import Literal

EntryKind = Literal["task", "project", "think", "note"]
EntryStatus = Literal["open", "done", "dropped"]


@dataclass(frozen=True)
class User:
    id: int
    tg_id: int
    name: str
    role: Literal["admin", "user"]
    brief_time: str
