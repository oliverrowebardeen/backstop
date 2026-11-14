"""Provider-neutral model calls using the approved httpx dependency.

Messages accept OpenAI-style tool turns or Anthropic content blocks. Tool
schemas accept either parameters or input_schema. Responses use ToolCall
models with id, name and parsed arguments. Callers emit the llm event.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from so import config
from so.contracts import LLMResponse, ToolCall

Role = Literal["agent", "checker", "judge"]

_cancelled_sink: ContextVar[list[LLMResponse] | None] = ContextVar("llm_cancelled_sink", default=None)


@contextmanager
def capture_cancelled_usage() -> Iterator[list[LLMResponse]]:
    """Collect the estimated usage of model calls cancelled after their request was sent.

    The provider bills a request it received even if the caller stops waiting, so
    such a call is settled at its reservation estimate and reported here (the list
    is shared with child tasks, so calls inside asyncio.wait_for are captured too).
    """
    sink: list[LLMResponse] = []
    token = _cancelled_sink.set(sink)
    try:
        yield sink
    finally:
        _cancelled_sink.reset(token)


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
    blocks: list[dict[str, Any]] = []
    if provider == "anthropic":
        content = raw.get("content", [])
        blocks = list(content)
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
                       latency_ms=latency_ms, model=model, role=role, stop_reason=stop, content=blocks)


async def complete(role: Role, system: str, messages: list[dict[str, Any]], *,
                   tools: list[dict[str, Any]] | None = None,
                   json_schema: dict[str, Any] | None = None) -> LLMResponse:
    """Complete a model turn with cached usage, bounded concurrency and spend."""
    models = {"checker": config.CHECKER_MODEL, "agent": config.AGENT_MODEL, "judge": config.JUDGE_MODEL}
    if role not in models:
        raise ValueError("role must be agent, checker or judge")
    provider = config.LLM_PROVIDER
    if role == "checker" and config.CHECKER_PROVIDER_EXPLICIT:
        provider = config.CHECKER_PROVIDER
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
        if config.LLM_THINKING.get(role):
            body["thinking"] = {"type": config.LLM_THINKING[role]}
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
            sent = False
            try:
                async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT_S) as client:
                    async def send() -> dict[str, Any]:
                        nonlocal sent
                        sent = True
                        response = await client.post(endpoint, headers=headers, json=body)
                        response.raise_for_status()
                        return response.json()
                    raw = await config.retry(send)
            except asyncio.CancelledError:
                # A request the provider received is billed even though nobody reads the reply.
                billed = estimate if sent else 0.0
                try:
                    config.LLM_BUDGET.settle(estimate, billed)
                except config.BudgetExceeded:
                    pass  # spend is recorded; cancellation must still propagate
                sink = _cancelled_sink.get()
                if sent and sink is not None:
                    sink.append(LLMResponse(
                        tokens_in=token_bound, tokens_out=max_tokens, cost_usd=billed, model=model, role=role,
                        latency_ms=(time.perf_counter() - start) * 1000, stop_reason="cancelled",
                    ))
                raise
            except BaseException:
                config.LLM_BUDGET.settle(estimate, 0)
                raise
            with config.paid_response(key, request, raw, config.LLM_BUDGET, estimate) as record:
                response = _normalize(raw, provider, role, model, (time.perf_counter() - start) * 1000)
                record.update(normalized=response.model_dump(mode="json"), _cost_usd=response.cost_usd)
            return response


class DecisionChoice(BaseModel):
    """One named option supplied to the Decisions API."""

    model_config = ConfigDict(strict=True, extra="forbid")
    value: str
    description: str


class DecisionResponse(LLMResponse):
    """A selected option and API confidence with the original billed usage."""

    choice: str
    confidence: float = Field(ge=0, le=1, strict=True)


class DecisionCallError(RuntimeError):
    """A failed decision, retaining billed or conservatively reserved usage."""

    def __init__(self, message: str, *, usage: LLMResponse, kind: str) -> None:
        super().__init__(message)
        self.usage = usage
        self.kind = kind


class _DecisionCacheUsage(BaseModel):
    model_config = ConfigDict(strict=True)
    cached_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    cache_write_tokens_details: dict[str, int] = Field(default_factory=dict)


class _DecisionUsage(BaseModel):
    model_config = ConfigDict(strict=True)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    input_tokens_details: _DecisionCacheUsage


def _decision_rates() -> dict[str, float]:
    """Require explicit, verified prices before making any paid decision."""
    rates = config.DECISION_PRICE_USD.get(config.DECISION_MODEL, {})
    for name in ("input", "cache_read", "cache_write", "cache_write_12h"):
        value = rates.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("Set verified Decisions rates in DECISION_PRICE_USD_JSON")
    return rates


def _decision_usage(raw: dict[str, Any], elapsed: float, rates: dict[str, float]) -> LLMResponse:
    usage = _DecisionUsage.model_validate(raw["usage"])
    details = usage.input_tokens_details
    cached, written = details.cached_tokens, details.cache_write_tokens
    long_write = details.cache_write_tokens_details.get("ttl_12h_tokens", 0)
    if not 0 <= long_write <= written or cached + written > usage.input_tokens:
        raise ValueError("Invalid Decisions cache token counts")
    cost = ((usage.input_tokens - cached - written) * rates["input"]
            + cached * rates["cache_read"] + (written - long_write) * rates["cache_write"]
            + long_write * rates["cache_write_12h"]) / 1_000_000
    return LLMResponse(role="checker", model=config.DECISION_MODEL, tokens_in=usage.input_tokens,
                       tokens_out=usage.output_tokens, cost_usd=cost, latency_ms=elapsed)


def _decision_answer(raw: dict[str, Any], choices: Sequence[DecisionChoice], usage: LLMResponse) -> DecisionResponse:
    answers = raw["answers"]
    if not isinstance(answers, list) or len(answers) != 1:
        raise ValueError("Expected exactly one Decisions answer")
    answer = answers[0]
    if not isinstance(answer, dict) or answer.get("name") != "checker":
        raise ValueError("Decisions answer name does not match the request")
    if answer.get("type") == "refusal":
        raise DecisionCallError("Decisions refused the checker request", usage=usage, kind="refusal")
    if answer.get("type") != "choice" or answer.get("choice") not in {c.value for c in choices}:
        raise ValueError("Decisions returned an unknown choice")
    return DecisionResponse(**usage.model_dump(), choice=answer["choice"], confidence=answer["confidence"])


async def decide(instructions: str, evidence: str, choices: Sequence[DecisionChoice]) -> DecisionResponse:
    """Make one OpenAI decision, cached and charged to both LLM and Decisions budgets.

    CHECKER_PROVIDER selects the ordinary LLM checker and timeout fallback.
    This endpoint is OpenAI-only and always uses OPENAI_API_KEY. A timeout is
    charged at its conservative reservation because delivery is uncertain.
    """
    choices = [DecisionChoice.model_validate(c) for c in choices]
    if not 2 <= len(choices) <= config.DECISION_MAX_CHOICES or len({c.value for c in choices}) != len(choices):
        raise ValueError("Decisions requires unique choices within DECISION_MAX_CHOICES")
    body = {"model": config.DECISION_MODEL, "input": evidence, "questions": [{
        "type": "choice", "name": "checker", "instructions": instructions,
        "choices": [c.model_dump() for c in choices],
    }]}
    size = len(json.dumps(body, ensure_ascii=False).encode()) + config.LLM_TOKEN_RESERVE_OVERHEAD
    if size > config.DECISION_MAX_INPUT_BYTES:
        raise ValueError("Decisions request exceeds DECISION_MAX_INPUT_BYTES")
    rates = _decision_rates()
    request = {"provider": "openai", "endpoint": config.DECISION_URL, "body": body}
    key = config.cache_key("decision-v2", request)
    async with config.semaphore(key, 1):
        cached = config.read_cache(key)
        if cached:
            if cached.get("failure"):
                usage = LLMResponse.model_validate(cached["failure_usage"]).model_copy(update={"cached": True})
                raise DecisionCallError("Cached Decisions response failed validation", usage=usage, kind=cached["failure"])
            response = DecisionResponse.model_validate(cached["normalized"])
            if response.choice not in {c.value for c in choices}:
                raise ValueError("Cached decision choice does not match the request")
            return response.model_copy(update={"cached": True})
        api_key = config.api_key("openai")
        estimate = size * max(rates.values()) / 1_000_000
        async with config.semaphore("decision", config.DECISION_MAX_CONCURRENCY):
            start = time.perf_counter()
            uncertain_cost = 0.0

            async def send() -> DecisionResponse:
                nonlocal uncertain_cost
                config.LLM_BUDGET.reserve(estimate)
                try:
                    config.DECISION_BUDGET.reserve(estimate)
                except BaseException:
                    config.LLM_BUDGET.settle(estimate, 0)
                    raise
                try:
                    async with httpx.AsyncClient(timeout=config.DECISION_TIMEOUT_S) as client:
                        response = await client.post(config.DECISION_URL,
                            headers={"Authorization": f"Bearer {api_key}"}, json=body)
                        response.raise_for_status()
                except BaseException as exc:
                    uncertain = isinstance(exc, (httpx.TimeoutException, asyncio.CancelledError))
                    charge = estimate if uncertain else 0
                    uncertain_cost += charge
                    try:
                        config.LLM_BUDGET.settle(estimate, charge)
                    finally:
                        config.DECISION_BUDGET.settle(estimate, charge)
                    raise
                elapsed = (time.perf_counter() - start) * 1000
                usage = LLMResponse(role="checker", model=config.DECISION_MODEL,
                                    cost_usd=estimate, latency_ms=elapsed)
                try:
                    raw = response.json()
                except ValueError:
                    raw = {"invalid_json": response.text}
                failure = None
                try:
                    with config.paid_response(key, request, raw, config.LLM_BUDGET, estimate) as record:
                        try:
                            usage = _decision_usage(raw, elapsed, rates)
                            record["_cost_usd"] = usage.cost_usd
                            result = _decision_answer(raw, choices, usage)
                            if raw.get("model") != config.DECISION_MODEL:
                                raise ValueError("Decisions response model does not match the requested model")
                        except (ValueError, KeyError, TypeError, AttributeError, DecisionCallError) as exc:
                            kind = exc.kind if isinstance(exc, DecisionCallError) else "malformed"
                            failure = DecisionCallError("Decisions response failed validation", usage=usage, kind=kind)
                            record.update(failure=kind, failure_usage=usage.model_dump(mode="json"))
                        else:
                            record["normalized"] = result.model_dump(mode="json")
                finally:
                    config.DECISION_BUDGET.settle(estimate, usage.cost_usd)
                if failure is not None:
                    raise failure
                return result

            try:
                async with asyncio.timeout(config.DECISION_TIMEOUT_S):
                    return await config.retry(send)
            except (TimeoutError, httpx.TimeoutException) as exc:
                usage = LLMResponse(role="checker", model=config.DECISION_MODEL, cost_usd=uncertain_cost,
                                    latency_ms=(time.perf_counter() - start) * 1000)
                config.write_cache(key, {"request": request, "failure": "timeout",
                                         "failure_usage": usage.model_dump(mode="json")})
                raise DecisionCallError("Decisions request timed out", usage=usage, kind="timeout") from exc
            except httpx.HTTPError as exc:
                usage = LLMResponse(role="checker", model=config.DECISION_MODEL,
                                    latency_ms=(time.perf_counter() - start) * 1000)
                status = getattr(getattr(exc, "response", None), "status_code", None)
                detail = f"HTTP {status}" if status else type(exc).__name__
                raise DecisionCallError(f"Decisions request failed: {detail}", usage=usage, kind="transport") from exc
