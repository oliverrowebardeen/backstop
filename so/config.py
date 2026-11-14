"""Environment, prices and shared cache/budget primitives.

Budgets count live spend within this Python process. Cached calls retain their
original reported cost but do not spend budget. Search prices are from
https://docs.telem.ai/pricing/ (2026-11-14); Exa uses the brief's estimate.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import json
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


load_env()
ENGINES = ["tavily", "exa", "brave", "perplexity", "parallel", "ceramic"]
FIRST_ENGINE = "tavily"
SUBSTITUTES = ["seltz", "you", "linkup"]
fallback_order = ["exa", "perplexity", "brave", "parallel", "ceramic"]
FALLBACK_ORDER = fallback_order
PRICE_USD = {"tavily": 0.005, "brave": 0.005, "you": 0.005,
             "parallel": 0.001, "ceramic": 0.00005, "tinyfish": 0.0,
             "perplexity": 0.005, "seltz": 0.005, "linkup": 0.005,
             "serpapi": 0.006}
DEFAULT_PRICE_USD = 0.005
TELEM_FETCH_PRICE_USD = 0.001  # Pin Telem fetch to Tavily for a known page price.
# Basic Extract: 1 credit per 5 successes; PAYG credit price is $0.008.
# https://docs.tavily.com/documentation/api-credits
TAVILY_CREDIT_USD = float(os.getenv("TAVILY_CREDIT_USD", "0.008"))
TAVILY_EXTRACT_PRICE_USD = TAVILY_CREDIT_USD / 5
SO_CACHE = os.getenv("SO_CACHE", "readwrite")
if SO_CACHE not in {"readwrite", "off"}:
    raise ValueError("SO_CACHE must be readwrite or off")
CACHE_DIR = Path(os.getenv("SO_CACHE_DIR", str(ROOT / "cache")))
TELEM_MAX_CONCURRENCY = int(os.getenv("TELEM_MAX_CONCURRENCY", "16"))
TELEM_BUDGET_USD = float(os.getenv("TELEM_BUDGET_USD", "25"))
LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "8"))
LLM_BUDGET_USD = float(os.getenv("LLM_BUDGET_USD", "25"))
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
CHECKER_MODEL = os.getenv("CHECKER_MODEL", "claude-haiku-5-5")
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-5-5")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "claude-sonnet-5-5")
# USD per million input/output tokens, verified in Claude pricing docs.
LLM_PRICE_USD = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0)}
# Set explicitly for OpenAI/custom models, e.g. {"model-id": [input, output]}.
LLM_PRICE_USD.update(json.loads(os.getenv("LLM_PRICE_USD_JSON", "{}")))


class BudgetExceeded(RuntimeError):
    """A call would exceed the process's configured live-spend budget."""


class Budget:
    def __init__(self, limit: float) -> None:
        if limit < 0:
            raise ValueError("Budget must be nonnegative")
        self.limit = limit
        self.spent = 0.0
        self.reserved = 0.0
        self._lock = threading.Lock()

    def reserve(self, amount: float) -> None:
        if amount < 0:
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
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


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


async def retry(call: Callable[[], Awaitable[T]]) -> T:
    """Initial attempt plus at most three retries for 429 or 5xx."""
    for attempt in range(4):
        try:
            return await call()
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status is None:
                status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt == 3 or status is None or not (status == 429 or 500 <= status < 600):
                raise
            await asyncio.sleep(0.5 * 2**attempt)
    raise AssertionError("Retry loop exhausted")
