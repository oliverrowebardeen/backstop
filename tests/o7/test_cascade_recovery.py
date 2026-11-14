"""Exercise cascade recovery through the real checker and model wrappers."""
import asyncio
import json
import os
import subprocess
import sys

import httpx
import pytest

from so import config, decision_checker as dc, llm
from so.contracts import Result

pytestmark = pytest.mark.checker_backend


@pytest.fixture
def cascade(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CHECKER_BACKEND", "cascade")
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "CHECKER_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "SO_CACHE", "off")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_BUDGET", config.Budget(1))
    monkeypatch.setattr(config, "DECISION_BUDGET", config.Budget(1))
    monkeypatch.setattr(config, "API_MAX_RETRIES", 0)
    monkeypatch.setattr(dc, "_fallback_warning_emitted", False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-placeholder")
    return [Result(rank=i, title="Leadership", url=f"https://example.org/{i}",
                   snippet="Ada is the CEO.", engine="tavily") for i in (1, 2)]


def install_transport(monkeypatch, failure, calls):
    async def handle(request):
        if str(request.url) == config.DECISION_URL:
            calls.append("decision")
            if failure == "timeout":
                await asyncio.sleep(1)
                raise AssertionError("Decisions timeout did not cancel the request")
            if failure == "transport":
                raise httpx.ConnectError("Connection failed", request=request)
            return httpx.Response(200, json={
                "model": config.DECISION_MODEL,
                "answers": [{"name": "checker", "type": "choice", "choice": "HIT_1",
                             "confidence": .9, "probabilities": []}],
                "usage": {"input_tokens": 1000, "output_tokens": 0,
                          "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}},
            })
        assert str(request.url) == config.ANTHROPIC_MESSAGES_URL
        calls.append("llm")
        return httpx.Response(200, json={
            "model": config.CHECKER_MODEL, "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps({
                "verdict": "HIT", "confidence": .9, "why": "The source names Ada.",
                "rank": 2, "rewrite": None,
            })}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        })

    original = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handle)))


@pytest.mark.parametrize("failure,reason", [
    ("timeout", "timeout"), ("missing_key", "missing_api_key"),
    ("budget", "decision_budget_exhausted"), ("transport", "transport"),
    ("storage", "DecisionStorageError"),
])
async def test_cascade_fallback_preserves_llm_pointers(cascade, monkeypatch, failure, reason):
    calls = []
    install_transport(monkeypatch, failure, calls)
    if failure == "timeout":
        monkeypatch.setattr(config, "DECISION_TIMEOUT_S", .01)
    elif failure == "missing_key":
        monkeypatch.setenv("OPENAI_API_KEY", "")
    elif failure == "budget":
        monkeypatch.setattr(config, "DECISION_BUDGET", config.Budget(0))
    elif failure == "storage":
        monkeypatch.setattr(config, "SO_CACHE", "readwrite")
        write_cache = config.write_cache

        def fail_decision_write(key, value):
            if key.startswith("decision-"):
                raise OSError("Storage unavailable")
            return write_cache(key, value)

        monkeypatch.setattr(config, "write_cache", fail_decision_write)

    verdict = await dc.configured_check("CEO", "Find the current CEO", cascade)

    assert verdict.verdict == "HIT" and verdict.fallback and verdict.escalated
    assert verdict.decision_failure == reason
    assert verdict.evidence_rank == 2 and verdict.best_url == cascade[1].url
    assert calls == (["llm"] if failure in {"missing_key", "budget"} else ["decision", "llm"])
    assert config.LLM_BUDGET.reserved == config.DECISION_BUDGET.reserved == 0
    assert verdict.cost_usd == pytest.approx(config.LLM_BUDGET.spent)


@pytest.mark.parametrize("missing_key", [False, True])
async def test_llm_budget_still_propagates(cascade, monkeypatch, missing_key):
    calls = []
    install_transport(monkeypatch, "budget", calls)
    monkeypatch.setattr(config, "LLM_BUDGET", config.Budget(0))
    monkeypatch.setattr(config, "DECISION_BUDGET", config.Budget(0))
    if missing_key:
        monkeypatch.setenv("OPENAI_API_KEY", "")

    with pytest.raises(config.BudgetExceeded) as caught:
        await dc.configured_check("CEO", "Find the current CEO", cascade)

    assert caught.value.budget is config.LLM_BUDGET
    assert calls == []


@pytest.mark.parametrize("backend,timeout,warning", [
    ("cascade", None, False), ("cascade", "2", False),
    ("cascade", "4", True), ("cascade", "10", True),
    ("llm", "10", False), ("decision", "10", False),
])
def test_cascade_timeout_order_warns_without_breaking_startup(tmp_path, backend, timeout, warning):
    env = {**os.environ, "SO_ENV_FILE": str(tmp_path / "absent.env"),
           "CHECKER_BACKEND": backend, "CHECKER_TIMEOUT_S": "4"}
    env.pop("DECISION_TIMEOUT_S", None)
    if timeout is not None:
        env["DECISION_TIMEOUT_S"] = timeout
    completed = subprocess.run(
        [sys.executable, "-c", "from so import config; print(config.DECISION_TIMEOUT_S)"],
        env=env, text=True, capture_output=True, check=True,
    )
    assert float(completed.stdout.strip()) == float(timeout or "2")
    assert ("DECISION_TIMEOUT_S should be less than CHECKER_TIMEOUT_S" in completed.stderr) is warning
