"""Stand-in for so.contracts.AgentAnswer (section 6.10) until O3 merges."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AgentAnswer(BaseModel):
    final: str
    status: Literal["ANSWERED", "NOT_FOUND"]
    citations: list[str] = []
    searches: int = 0
