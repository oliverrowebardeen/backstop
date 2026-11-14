import json

import httpx
import pytest

from so import config, llm


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "SO_CACHE", "readwrite")
    monkeypatch.setattr(config, "LLM_BUDGET", config.Budget(1))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-placeholder")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_provider_payload_normalization_and_cache(provider, monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", provider)
    model = "claude-haiku-5-5" if provider == "anthropic" else "test-model"
    monkeypatch.setattr(config, "CHECKER_MODEL", model)
    monkeypatch.setitem(config.LLM_PRICE_USD, model, {"input": 0.1, "output": 0.5})
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if provider == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": '{"ok":true}'}],
                "usage": {"input_tokens": 100, "output_tokens": 10}, "stop_reason": "end_turn"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    original = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond)))
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    first = await llm.complete("checker", "Check", [{"role": "user", "content": "answer"}], json_schema=schema)
    cached = await llm.complete("checker", "Check", [{"role": "user", "content": "answer"}], json_schema=schema)
    assert first.text == '{"ok":true}' and first.tokens_in == 100 and first.tokens_out == 10
    assert first.cost_usd == pytest.approx(0.000015)
    assert len(requests) == 1 and cached.cached
    assert cached.latency_ms == first.latency_ms
    assert config.LLM_BUDGET.spent == first.cost_usd
    assert requests[0]["model"] == model
    if provider == "anthropic":
        assert requests[0]["max_tokens"] == 399
        assert requests[0]["output_config"]["format"]["schema"] == schema
    else:
        assert requests[0]["max_completion_tokens"] == 399
        assert requests[0]["response_format"]["json_schema"]["schema"] == schema


def test_tool_turns_round_trip_across_providers():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "search", "arguments": {"query": "q"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    native = llm._anthropic_messages(messages)
    assert native[0]["content"][0]["input"] == {"query": "q"}
    restored = llm._openai_messages(native)
    assert json.loads(restored[0]["tool_calls"][0]["function"]["arguments"]) == {"query": "q"}
    assert restored[1] == messages[1]
    response = llm._normalize({"content": native[0]["content"], "usage": {"input_tokens": 100, "output_tokens": 20}},
                              "anthropic", "agent", "claude-sonnet-5-5", 12)
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].arguments == {"query": "q"}


async def test_unknown_price_prevents_paid_call(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MODEL", "unpriced-model")
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    with pytest.raises(ValueError, match="verified input/output prices"):
        await llm.complete("agent", "s", [{"role": "user", "content": "u"}])
    assert config.LLM_BUDGET.spent == 0


def test_haiku_long_context_price_and_cached_tokens():
    response = llm._normalize({"content": [], "usage": {"input_tokens": 100001, "output_tokens": 10,
                              "cache_read_input_tokens": 100, "cache_creation_input_tokens": 100}},
                             "anthropic", "checker", "claude-haiku-5-5", 1)
    assert response.tokens_in == 100201
    assert response.cost_usd == pytest.approx((100001 * .5 + 100 * .05 + 100 * .625 + 10 * 2.5) / 1e6)


def test_pydantic_schema_adaptation_preserves_field_names_and_input():
    from so.contracts import CheckerVerdict
    original = CheckerVerdict.model_json_schema()
    strict = llm._strict_schema(original)
    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == set(original["properties"])
    assert "minimum" not in strict["properties"]["confidence"]
    assert "maximum" in strict["properties"]["confidence"]["description"]
    assert original["properties"]["confidence"]["minimum"] == 0
    named_default = {"type": "object", "properties": {"default": {"type": "string", "default": "x"}}}
    assert llm._strict_schema(named_default)["properties"]["default"] == {"type": "string"}


def test_custom_model_price_factors_drive_both_providers(monkeypatch):
    monkeypatch.setitem(config.LLM_PRICE_USD, "custom", {"input": 2, "output": 4, "cache_read_factor": .25,
        "cache_write_factor": 1.5, "cache_write_1h_factor": 3,
        "long_context_threshold": 100, "long_input": 5, "long_output": 8})
    assert llm._rates("custom", 100) == (2, 4)
    assert llm._rates("custom", 101) == (5, 8)
    response = llm._normalize({"content": [], "usage": {"input_tokens": 80, "output_tokens": 10,
        "cache_read_input_tokens": 20, "cache_creation_input_tokens": 10,
        "cache_creation": {"ephemeral_1h_input_tokens": 4}}}, "anthropic", "agent", "custom", 0)
    assert response.cost_usd == pytest.approx((5 * (80 + 20 * .25 + 6 * 1.5 + 4 * 3) + 10 * 8) / 1e6)
    response = llm._normalize({"choices": [{"message": {"content": "OK"}}], "usage": {
        "prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 20}}},
        "openai", "agent", "custom", 0)
    assert response.cost_usd == pytest.approx((2 * (80 + 20 * .25) + 10 * 4) / 1e6)
