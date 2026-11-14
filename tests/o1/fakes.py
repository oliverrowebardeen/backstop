"""Scripted stand-ins for so.llm and so.eventlog used by the O1 tests."""
from __future__ import annotations

from typing import Any

from so.contracts import LLMResponse


class ScriptedModel:
    """Replaces so.checker._call_model. Replies are returned in order.

    A reply is text, a (text, stop_reason) pair, or an Exception to raise.
    """

    def __init__(self, *replies: str | tuple[str, str] | Exception, cost_usd: float = 0.0002) -> None:
        self.replies = list(replies)
        self.cost_usd = cost_usd
        self.calls: list[tuple[str, str]] = []
        self.schemas: list[dict[str, Any] | None] = []

    async def __call__(self, system: str, user: str, schema: dict[str, Any] | None = None) -> LLMResponse:
        self.calls.append((system, user))
        self.schemas.append(schema)
        reply = self.replies.pop(0) if self.replies else ""
        if isinstance(reply, Exception):
            raise reply
        text, stop = reply if isinstance(reply, tuple) else (reply, "end_turn")
        return LLMResponse(text=text, tokens_in=900, tokens_out=60, cost_usd=self.cost_usd, latency_ms=420,
                           model="fake-checker", role="checker", stop_reason=stop)


class RecordingLog:
    """Captures emit() calls with the so.eventlog.EventLog signature."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, type: str, **data: Any) -> None:
        self.events.append((type, data))
