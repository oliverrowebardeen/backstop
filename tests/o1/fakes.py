"""Scripted stand-ins for so.llm and so.eventlog used by the O1 tests."""
from __future__ import annotations

from typing import Any

from so.contracts import LLMResponse


class ScriptedModel:
    """Replaces so.checker._call_model. Replies are returned in order; an Exception is raised."""

    def __init__(self, *replies: str | Exception, cost_usd: float = 0.0002) -> None:
        self.replies = list(replies)
        self.cost_usd = cost_usd
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str) -> LLMResponse:
        self.calls.append((system, user))
        reply = self.replies.pop(0) if self.replies else ""
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(text=reply, tokens_in=900, tokens_out=60, cost_usd=self.cost_usd,
                           latency_ms=420, model="fake-checker", role="checker")


class RecordingLog:
    """Captures emit() calls with the so.eventlog.EventLog signature."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, type: str, **data: Any) -> None:
        self.events.append((type, data))
