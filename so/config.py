"""Environment, prices and shared cache/budget primitives.

Budgets count live spend within this Python process. Cached calls retain their
original reported cost but do not spend budget. Search prices are from
https://docs.telem.ai/pricing/ (2026-11-14); Exa uses DEFAULT_PRICE_USD unless overridden.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import tempfile
import threading
from typing import Any, TypeVar
from weakref import WeakKeyDictionary

ROOT = Path(__file__).resolve().parent.parent
T = TypeVar("T")


def load_env(path: Path = ROOT / ".env") -> None:
    """Read dotenv assignments without executing shell or overriding exports."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key.isidentifier():
            parts = shlex.split(value, comments=True)
            os.environ.setdefault(key, " ".join(parts))


def _csv(name: str, default: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in os.getenv(name, default).split(",") if item.strip()))


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def api_key(provider: str) -> str:
    """Read a configured credential without exposing its value in errors."""
    name = f"{provider.upper()}_API_KEY"
    value = os.getenv(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


load_env(Path(os.getenv("SO_ENV_FILE", str(ROOT / ".env"))))
ENGINES = _csv("ENGINES", "tavily,exa,brave,perplexity,parallel,ceramic")
FIRST_ENGINE = os.getenv("FIRST_ENGINE", "tavily")
SUBSTITUTES = _csv("SUBSTITUTES", "seltz,you,linkup")
fallback_order = _csv("FALLBACK_ORDER", "exa,perplexity,brave,parallel,ceramic")
FALLBACK_ORDER = fallback_order
PRICE_USD = {"tavily": 0.005, "brave": 0.005, "you": 0.005,
             "parallel": 0.001, "ceramic": 0.00005, "tinyfish": 0.0,
             "perplexity": 0.005, "seltz": 0.005, "linkup": 0.005,
             "serpapi": 0.006}
PRICE_USD.update(json.loads(os.getenv("PRICE_USD_JSON", "{}")))
DEFAULT_PRICE_USD = float(os.getenv("DEFAULT_PRICE_USD", "0.005"))
TELEM_FETCH_PROVIDER = os.getenv("TELEM_FETCH_PROVIDER", "tavily")
TELEM_FETCH_PRICE_USD = float(os.getenv("TELEM_FETCH_PRICE_USD", "0.001"))
TELEM_ROUTING_ENGINES = _csv("TELEM_ROUTING_ENGINES", ",".join(dict.fromkeys(ENGINES + SUBSTITUTES + list(PRICE_USD))))
TELEM_BASE_URL = os.getenv("TELEM_BASE_URL", "https://router.telem.ai")
TELEM_TIMEOUT_S = float(os.getenv("TELEM_TIMEOUT_S", "60"))
TELEM_TIER = os.getenv("TELEM_TIER", "default")
TELEM_NUM_RESULTS = int(os.getenv("TELEM_NUM_RESULTS", "5"))
RESULT_SNIPPET_CHARS = int(os.getenv("RESULT_SNIPPET_CHARS", "500"))
PAGE_TEXT_CHARS = int(os.getenv("PAGE_TEXT_CHARS", "6000"))
PAGE_CONTENT_FORMAT = os.getenv("PAGE_CONTENT_FORMAT", "text")
# Basic Extract: 1 credit per 5 successes; PAYG credit price is $0.008.
# https://docs.tavily.com/documentation/api-credits
TAVILY_CREDIT_USD = float(os.getenv("TAVILY_CREDIT_USD", "0.008"))
TAVILY_EXTRACT_PRICE_USD = float(os.getenv("TAVILY_EXTRACT_PRICE_USD", str(TAVILY_CREDIT_USD / 5)))
TAVILY_API_URL = os.getenv("TAVILY_API_URL", "https://api.tavily.com")
TAVILY_TIMEOUT_S = float(os.getenv("TAVILY_TIMEOUT_S", "30"))
TAVILY_EXTRACT_DEPTH = os.getenv("TAVILY_EXTRACT_DEPTH", "basic")
SO_CACHE = os.getenv("SO_CACHE", "readwrite")
if SO_CACHE not in {"readwrite", "off"}:
    raise ValueError("SO_CACHE must be readwrite or off")
CACHE_DIR = Path(os.getenv("SO_CACHE_DIR", str(ROOT / "cache")))
OUT_DIR = Path(os.getenv("SO_OUT_DIR", str(ROOT / "out")))
SO_OUT_DIR = OUT_DIR
SO_LOG_DIR = Path(os.getenv("SO_LOG_DIR", str(OUT_DIR / "logs")))
LOG_DIR = SO_LOG_DIR
AGENT_MAX_SEARCHES = int(os.getenv("AGENT_MAX_SEARCHES", "6"))
AGENT_MAX_PAGE_READS = int(os.getenv("AGENT_MAX_PAGE_READS", "12"))
AGENT_MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "24"))
AGENT_PROMPT_PATH = Path(os.getenv("AGENT_PROMPT_PATH", str(ROOT / "so" / "prompts" / "agent.md")))
SO_ENABLE_SECOND_OPINION = _bool("SO_ENABLE_SECOND_OPINION", True)
MAX_RESCUES = int(os.getenv("MAX_RESCUES", "2"))
FRESHNESS_DAYS = int(os.getenv("FRESHNESS_DAYS", "90"))
RESCUE_TABLE_PATH = Path(os.getenv("RESCUE_TABLE_PATH", str(OUT_DIR / "rescue_table.json")))
TELEM_MAX_CONCURRENCY = int(os.getenv("TELEM_MAX_CONCURRENCY", "16"))
TELEM_BUDGET_USD = float(os.getenv("TELEM_BUDGET_USD", "25"))
LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "8"))
LLM_BUDGET_USD = float(os.getenv("LLM_BUDGET_USD", "25"))
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "90"))
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
ANTHROPIC_MESSAGES_URL = os.getenv("ANTHROPIC_MESSAGES_URL", ANTHROPIC_BASE_URL.rstrip("/") + "/v1/messages")
OPENAI_CHAT_URL = os.getenv("OPENAI_CHAT_URL", OPENAI_BASE_URL.rstrip("/") + "/chat/completions")
ANTHROPIC_API_VERSION = os.getenv("ANTHROPIC_API_VERSION", "2023-06-01")
LLM_MAX_TOKENS = {"checker": 399, "agent": 2048, "judge": 1024}
LLM_MAX_TOKENS.update(json.loads(os.getenv("LLM_MAX_TOKENS_JSON", "{}")))
LLM_TOKEN_RESERVE_OVERHEAD = int(os.getenv("LLM_TOKEN_RESERVE_OVERHEAD", "1024"))
API_MAX_RETRIES = int(os.getenv("API_MAX_RETRIES", "3"))
API_RETRY_BACKOFF_S = float(os.getenv("API_RETRY_BACKOFF_S", "0.5"))
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
CHECKER_MODEL = os.getenv("CHECKER_MODEL", "claude-haiku-5-5")
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-5-5")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "claude-sonnet-5-5")
# USD per million input/output tokens, verified in Claude pricing docs.
LLM_PRICE_USD = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read_factor": 0.1,
        "cache_write_factor": 1.25, "cache_write_1h_factor": 2.0,
        "long_context_threshold": 100_000, "long_input": 0.50, "long_output": 2.50},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read_factor": 0.05,
        "cache_write_factor": 1.25, "cache_write_1h_factor": 2.0},
}
for _model, _prices in json.loads(os.getenv("LLM_PRICE_USD_JSON", "{}")).items():
    if isinstance(_prices, list):
        _prices = {"input": _prices[0], "output": _prices[1]}
    LLM_PRICE_USD[_model] = {**LLM_PRICE_USD.get(_model, {}), **_prices}


class BudgetExceeded(RuntimeError):
    """A call would exceed the process's configured live-spend budget."""


class Budget:
    def __init__(self, limit: float) -> None:
        if not math.isfinite(limit) or limit < 0:
            raise ValueError("Budget must be nonnegative")
        self.limit = limit
        self.spent = 0.0
        self.reserved = 0.0
        self._lock = threading.Lock()

    def reserve(self, amount: float) -> None:
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("Reservation must be nonnegative")
        with self._lock:
            if self.spent + self.reserved + amount > self.limit + 1e-12:
                raise BudgetExceeded(f"Budget ${self.limit:.4f} exhausted; spent ${self.spent:.4f}")
            self.reserved += amount

    def settle(self, reserved: float, actual: float) -> None:
        with self._lock:
            self.reserved = max(0, self.reserved - reserved)
            self.spent += actual
            if self.spent > self.limit + 1e-12:
                raise BudgetExceeded(f"Spend ${self.spent:.4f} exceeded budget ${self.limit:.4f}")


SEARCH_BUDGET = Budget(TELEM_BUDGET_USD)
LLM_BUDGET = Budget(LLM_BUDGET_USD)
_loop_resources: WeakKeyDictionary = WeakKeyDictionary()


def semaphore(name: str, limit: int) -> asyncio.Semaphore:
    if limit < 1:
        raise ValueError("Concurrency must be positive")
    resources = _loop_resources.setdefault(asyncio.get_running_loop(), {})
    return resources.setdefault((name, limit), asyncio.Semaphore(limit))


def cache_key(namespace: str, request: dict[str, Any]) -> str:
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{namespace}-{hashlib.sha256(encoded.encode()).hexdigest()}"


def read_cache(key: str) -> dict[str, Any] | None:
    if SO_CACHE == "off":
        return None
    path = CACHE_DIR / f"{key}.json"
    try:
        entry = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid cache JSON: {path.name}") from exc
    if entry.get("normalization_error"):
        raise ValueError(f"Cached provider response failed validation: {entry['normalization_error']}; use SO_CACHE=off to retry")
    return entry


def write_cache(key: str, value: dict[str, Any]) -> None:
    if SO_CACHE == "off":
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=CACHE_DIR, delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.replace(tmp, CACHE_DIR / f"{key}.json")
        finally:
            tmp.unlink(missing_ok=True)


@contextmanager
def paid_response(key: str, request: dict[str, Any], raw: dict[str, Any],
                  budget: Budget, reserved: float) -> Iterator[dict[str, Any]]:
    """Persist paid responses and charge even if normalization or cache writing fails.

    If usage cannot be decoded, retain the conservative reservation as spend.
    """
    record: dict[str, Any] = {"request": request, "raw": raw}
    try:
        yield record
    except Exception as exc:
        record["normalization_error"] = type(exc).__name__
        raise ValueError(f"Provider response failed validation: {type(exc).__name__}") from exc
    finally:
        cost = record.pop("_cost_usd", reserved)
        try:
            write_cache(key, record)
        finally:
            budget.settle(reserved, cost)


async def retry(call: Callable[[], Awaitable[T]]) -> T:
    """Retry 429 and 5xx responses using the configured cap and backoff."""
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            return await call()
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status is None:
                status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt == API_MAX_RETRIES or status is None or not (status == 429 or 500 <= status < 600):
                raise
            await asyncio.sleep(API_RETRY_BACKOFF_S * 2**attempt)
    raise AssertionError("Retry loop exhausted")


# Server
SERVER_UI_DIR = Path(os.getenv("SERVER_UI_DIR", str(ROOT / "ui")))
SERVER_DEMO_DIR = Path(os.getenv("SERVER_DEMO_DIR", str(ROOT / "demo")))
_server_fixture_dir = os.getenv("SERVER_FIXTURE_DIR", "").strip()
SERVER_FIXTURE_DIR = Path(_server_fixture_dir) if _server_fixture_dir else None
SERVER_LOG_DIR = Path(os.getenv("SERVER_LOG_DIR", str(SO_LOG_DIR)))
SERVER_RESULTS_PATH = Path(os.getenv("SERVER_RESULTS_PATH", str(OUT_DIR / "results.json")))
SERVER_FRONTIER_PATH = Path(os.getenv("SERVER_FRONTIER_PATH", str(OUT_DIR / "frontier.json")))
SERVER_MAX_ACTIVE_RUNS = int(os.getenv("SERVER_MAX_ACTIVE_RUNS", "8"))
SERVER_MAX_QUESTION_CHARS = int(os.getenv("SERVER_MAX_QUESTION_CHARS", "2000"))
SERVER_START_TIMEOUT_SECONDS = float(os.getenv("SERVER_START_TIMEOUT_SECONDS", "10"))
SERVER_RUN_TIMEOUT_SECONDS = float(os.getenv("SERVER_RUN_TIMEOUT_SECONDS", "180"))
SERVER_POLL_SECONDS = float(os.getenv("SERVER_POLL_SECONDS", "0.05"))
SERVER_KEEPALIVE_SECONDS = float(os.getenv("SERVER_KEEPALIVE_SECONDS", "10"))
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))
if SERVER_MAX_ACTIVE_RUNS < 2 or SERVER_MAX_QUESTION_CHARS < 1:
    raise ValueError("SERVER_MAX_ACTIVE_RUNS must be at least 2 and SERVER_MAX_QUESTION_CHARS must be positive")
for _server_setting in ("SERVER_START_TIMEOUT_SECONDS", "SERVER_RUN_TIMEOUT_SECONDS",
                        "SERVER_POLL_SECONDS", "SERVER_KEEPALIVE_SECONDS"):
    if not math.isfinite(globals()[_server_setting]) or globals()[_server_setting] <= 0:
        raise ValueError(f"{_server_setting} must be finite and positive")
if not SERVER_HOST.strip() or not 1 <= SERVER_PORT <= 65535:
    raise ValueError("SERVER_HOST must be nonempty and SERVER_PORT must be between 1 and 65535")
