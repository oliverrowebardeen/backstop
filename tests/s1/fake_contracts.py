"""Stand-in for so.contracts.Question until O0 merges (section 6.1).

data.load imports so.contracts first and only falls back to this file.
Keep the fields identical to SHARED_CONTEXT.md section 6.1.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Question(BaseModel):
    id: str
    question: str
    gold: list[str]
    category: Literal["fact", "finance", "trap"]
    source: str
    split: Literal["learn", "test"]
    is_trap: bool = False
    trap_note: str | None = None
