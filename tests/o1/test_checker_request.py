"""The checker's real request path through so.llm, with HTTP mocked.

Regression for the truncated-verdict bug: Haiku 5.5 thinks adaptively by
default, its hidden thinking counted against the checker's max_tokens, and
replies came back as an empty thinking block with stop_reason max_tokens.
"""
from __future__ import annotations

import json

import httpx
import pytest

from so import checker, config, llm
from tests.o1.examples import EXAMPLES, TODAY

CASE = EXAMPLES[0]
VALID = json.dumps({"verdict": "HIT", "confidence": 0.95, "why": "Result 1 gives the date.", "rank": 1,
                    "rewrite": None})


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "SO_CACHE", "readwrite")
    monkeypatch.setattr(config, "LLM_BUDGET", config.Budget(1))
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "CHECKER_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-placeholder")


def mock_api(monkeypatch, *bodies: dict) -> list[dict]:
    """Serve the given Messages API bodies in order and record each request."""
    requests: list[dict] = []
    queue = list(bodies)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=queue.pop(0))

    original = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond)))
    return requests


def text_reply(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 2400, "output_tokens": 95}}


THINKING_ONLY = {"content": [{"type": "thinking", "thinking": ""}], "stop_reason": "max_tokens",
                 "usage": {"input_tokens": 2400, "output_tokens": 399}}


async def test_checker_request_disables_thinking_and_uses_the_configured_cap(monkeypatch):
    monkeypatch.setitem(config.LLM_MAX_TOKENS, "checker", 222)
    requests = mock_api(monkeypatch, text_reply(VALID))
    out = await checker.check(CASE["query"], CASE["goal"], CASE["results"], TODAY)
    body = requests[0]
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 222
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert out.verdict == "HIT" and out.tokens_out == 95


async def test_thinking_mode_is_configurable(monkeypatch):
    monkeypatch.setitem(config.LLM_THINKING, "checker", "")
    requests = mock_api(monkeypatch, text_reply(VALID))
    await checker.check(CASE["query"], CASE["goal"], CASE["results"], TODAY)
    assert "thinking" not in requests[0]


async def test_thinking_only_truncated_reply_is_retried_then_an_operational_error(monkeypatch):
    requests = mock_api(monkeypatch, THINKING_ONLY, dict(THINKING_ONLY))
    with pytest.raises(checker.CheckerError, match=checker.TRUNCATED_WHY) as error:
        await checker.check(CASE["query"], CASE["goal"], CASE["results"], TODAY)
    assert len(requests) == 2
    assert error.value.kind == "truncated"
    assert error.value.usage.tokens_out == 798


async def test_truncated_reply_then_valid_reply_recovers(monkeypatch):
    requests = mock_api(monkeypatch, THINKING_ONLY, text_reply(VALID))
    out = await checker.check(CASE["query"], CASE["goal"], CASE["results"], TODAY)
    assert len(requests) == 2 and out.verdict == "HIT"
