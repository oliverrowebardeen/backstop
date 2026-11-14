"""Provider-neutral model calls using the approved httpx dependency.

Messages accept OpenAI-style tool turns or Anthropic content blocks. Tool
schemas accept either parameters or input_schema. Responses use ToolCall
models with id, name and parsed arguments. Callers emit the llm event.
"""
from __future__ import annotations

import json
import time
from typing import Any, Literal

import httpx

from so import config
from so.contracts import LLMResponse, ToolCall

Role = Literal["agent", "checker", "judge"]


def _strict_schema(value: Any) -> Any:
    """Adapt Pydantic schemas to the providers' shared structured-output subset."""
    if isinstance(value, list):
        return [_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    unsupported = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                   "minLength", "maxLength", "pattern", "format", "minItems", "maxItems", "uniqueItems"}
    schema = {}
    constraints = {}
    for key, item in value.items():
        if key == "default":
            continue
        if key in unsupported:
            constraints[key] = item
        elif key in {"properties", "$defs", "definitions"}:
            schema[key] = {name: _strict_schema(child) for name, child in item.items()}
        else:
            schema[key] = _strict_schema(item)
    if constraints:
        schema["description"] = (schema.get("description", "") +
                                  " Constraints: " + json.dumps(constraints, sort_keys=True)).strip()
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        if "properties" in schema:
            schema["required"] = list(schema["properties"])
    return schema


def _tool(tool: dict[str, Any]) -> dict[str, Any]:
    value = tool.get("function", tool)
    return {"name": value["name"], "description": value.get("description", ""),
            "input_schema": value.get("input_schema", value.get("parameters", {"type": "object", "properties": {}}))}


def _call(value: dict[str, Any]) -> ToolCall:
    function = value.get("function", value)
    arguments = function.get("arguments", function.get("input", {}))
    return ToolCall(id=value["id"], name=function["name"],
                    arguments=json.loads(arguments) if isinstance(arguments, str) else arguments)


def _anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        if message["role"] == "tool":
            result.append({"role": "user", "content": [{"type": "tool_result",
                           "tool_use_id": message["tool_call_id"], "content": message.get("content", "")}]})
        elif message.get("tool_calls"):
            content = message.get("content") or []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            blocks = list(content)
            for value in message["tool_calls"]:
                call = _call(value)
                blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
            result.append({"role": "assistant", "content": blocks})
        else:
            result.append({"role": message["role"], "content": message.get("content") or ""})
    return result


def _openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            text = "\n".join(b["text"] for b in content if b.get("type") == "text")
            calls = [{"id": b["id"], "name": b["name"], "arguments": b["input"]}
                     for b in content if b.get("type") == "tool_use"]
            tool_results = [b for b in content if b.get("type") == "tool_result"]
            if text or calls:
                result.extend(_openai_messages([dict(role=message["role"], content=text, tool_calls=calls)]))
            for block in tool_results:
                result.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                               "content": block.get("content", "")})
            continue
        converted = dict(message)
        if message.get("tool_calls"):
            converted["tool_calls"] = []
            for value in message["tool_calls"]:
                call = _call(value)
                converted["tool_calls"].append({"id": call.id, "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)}})
        elif "tool_calls" in converted:
            del converted["tool_calls"]
        result.append(converted)
    return result


def _rates(model: str, tokens_in: int = 0) -> tuple[float, float]:
    if model not in config.LLM_PRICE_USD:
        raise ValueError(f"Set verified input/output prices for {model} in LLM_PRICE_USD_JSON")
    rates = config.LLM_PRICE_USD[model]
    if tokens_in > rates.get("long_context_threshold", float("inf")):
        return float(rates["long_input"]), float(rates["long_output"])
    return float(rates["input"]), float(rates["output"])


def _normalize(raw: dict[str, Any], provider: str, role: Role, model: str, latency_ms: float) -> LLMResponse:
    usage = raw["usage"]
    if provider == "anthropic":
        content = raw.get("content", [])
        text = "".join(block["text"] for block in content if block.get("type") == "text")
        calls = [_call(block) for block in content if block.get("type") == "tool_use"]
        plain_in = usage["input_tokens"]
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
        tokens_in = plain_in + cache_read + cache_write
        tokens_out = usage["output_tokens"]
        input_rate, output_rate = _rates(model, tokens_in)
        prices = config.LLM_PRICE_USD[model]
        creation = usage.get("cache_creation") or {}
        hour_write = creation.get("ephemeral_1h_input_tokens", 0)
        cost_in = input_rate * (plain_in + cache_read * prices.get("cache_read_factor", 1)
            + (cache_write - hour_write) * prices.get("cache_write_factor", 1)
            + hour_write * prices.get("cache_write_1h_factor", 1))
        stop = raw.get("stop_reason")
    else:
        choice = raw["choices"][0]
        message = choice["message"]
        text = message.get("content") or ""
        calls = [_call(call) for call in message.get("tool_calls", [])]
        tokens_in, tokens_out = usage["prompt_tokens"], usage["completion_tokens"]
        input_rate, output_rate = _rates(model, tokens_in)
        cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        read_factor = config.LLM_PRICE_USD[model].get("cache_read_factor", 1)
        cost_in = input_rate * (tokens_in - cached_tokens + cached_tokens * read_factor)
        stop = choice.get("finish_reason")
    return LLMResponse(text=text, tool_calls=calls, tokens_in=tokens_in, tokens_out=tokens_out,
                       cost_usd=(cost_in + tokens_out * output_rate) / 1_000_000,
                       latency_ms=latency_ms, model=model, role=role, stop_reason=stop)


async def complete(role: Role, system: str, messages: list[dict[str, Any]], *,
                   tools: list[dict[str, Any]] | None = None,
                   json_schema: dict[str, Any] | None = None) -> LLMResponse:
    """Complete a model turn with cached usage, bounded concurrency and spend."""
    models = {"checker": config.CHECKER_MODEL, "agent": config.AGENT_MODEL, "judge": config.JUDGE_MODEL}
    if role not in models:
        raise ValueError("role must be agent, checker or judge")
    provider = config.LLM_PROVIDER
    if provider not in {"anthropic", "openai"}:
        raise ValueError("LLM_PROVIDER must be anthropic or openai")
    model = models[role]
    max_tokens = config.LLM_MAX_TOKENS[role]
    body: dict[str, Any] = {"model": model}
    if json_schema:
        json_schema = _strict_schema(json_schema)
    if provider == "anthropic":
        endpoint = config.ANTHROPIC_MESSAGES_URL
        body.update(system=system, messages=_anthropic_messages(messages), max_tokens=max_tokens)
        if tools:
            body["tools"] = [_tool(tool) for tool in tools]
        if json_schema:
            body["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}
        headers = {"anthropic-version": config.ANTHROPIC_API_VERSION}
    else:
        endpoint = config.OPENAI_CHAT_URL
        body.update(messages=[{"role": "system", "content": system}] + _openai_messages(messages),
                    max_completion_tokens=max_tokens)
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"],
                "description": t["description"], "parameters": t["input_schema"]}} for t in map(_tool, tools)]
        if json_schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "response", "schema": json_schema, "strict": True}}
        headers = {}
    request = {"provider": provider, "endpoint": endpoint, "role": role, "body": body}
    key = config.cache_key("llm-v1", request)
    async with config.semaphore(key, 1):
        cached = config.read_cache(key)
        if cached:
            return LLMResponse.model_validate(cached["normalized"]).model_copy(update={"cached": True})
        api_key = config.api_key(provider)
        if provider == "anthropic":
            headers["x-api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
        # UTF-8 byte length plus framing overhead is a conservative token reservation.
        token_bound = len(json.dumps(body, ensure_ascii=False).encode()) + config.LLM_TOKEN_RESERVE_OVERHEAD
        input_rate, output_rate = _rates(model, token_bound)
        estimate = (token_bound * input_rate + max_tokens * output_rate) / 1_000_000
        async with config.semaphore("llm", config.LLM_MAX_CONCURRENCY):
            config.LLM_BUDGET.reserve(estimate)
            start = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT_S) as client:
                    async def send() -> dict[str, Any]:
                        response = await client.post(endpoint, headers=headers, json=body)
                        response.raise_for_status()
                        return response.json()
                    raw = await config.retry(send)
            except BaseException:
                config.LLM_BUDGET.settle(estimate, 0)
                raise
            with config.paid_response(key, request, raw, config.LLM_BUDGET, estimate) as record:
                response = _normalize(raw, provider, role, model, (time.perf_counter() - start) * 1000)
                record.update(normalized=response.model_dump(mode="json"), _cost_usd=response.cost_usd)
            return response
