"""Hosted verified-search API: POST /v1/search, GET /v1/usage, GET /health.

Authentication is `Authorization: Bearer <key>`, checked against the
comma-separated BACKSTOP_API_KEYS. Keys and request bodies are never logged.
Errors are JSON: {"detail": {"code": ..., "message": ...}} (422 uses FastAPI's
standard validation detail list).

Tenancy: every key has a lifetime spend cap (BACKSTOP_KEY_SPEND_CAP_USD). A
request first reserves its worst-case cost against what the key has left, so
concurrent requests cannot together pass the cap; past it the answer is 402.
Per-request max_cost_usd and max_rescues are clamped to BACKSTOP_MAX_COST_USD
and BACKSTOP_MAX_RESCUES. Telem sessions belong to the key whose search
returned them: a client-supplied session on /v1/search answers 404 for any
other key, the same as for a session that does not exist.
"""

from __future__ import annotations

import hmac
import logging
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.usage import RateLimiter, UsageStore, key_id
from so import config
from so.client import BackstopSearchResponse, BackstopTelem
from so.second_opinion import TelemUnavailable

VERSION = "0.1.0"
logger = logging.getLogger(__name__)
router = APIRouter()


class SearchRequest(BaseModel):
    """Body of POST /v1/search: Telem's search fields plus optional per-call limits."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    goal: str | None = Field(default=None, max_length=2000)
    session: str | None = Field(default=None, max_length=200)
    max_rescues: int | None = Field(default=None, ge=0, le=5)
    max_cost_usd: float | None = Field(default=None, gt=0, le=1)
    timeout_s: float | None = Field(default=None, gt=0, le=120)
    checker_timeout_s: float | None = Field(default=None, gt=0, le=60)

    @field_validator("query")
    @classmethod
    def _query_has_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must contain non-whitespace text")
        return value

    @field_validator("goal", "session")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None


_SESSION_NOT_FOUND = "No Telem session with this id belongs to this API key."


def _spend_cap_error() -> HTTPException:
    return _error(402, "spend_cap_reached",
                  f"This API key has reached its spend cap of ${config.BACKSTOP_KEY_SPEND_CAP_USD:.2f}.")


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message}, headers=headers)


def configured_keys() -> list[str]:
    """API keys accepted by this deployment, read from BACKSTOP_API_KEYS."""
    return [key.strip() for key in os.getenv("BACKSTOP_API_KEYS", "").split(",") if key.strip()]


@lru_cache(maxsize=1)
def get_usage_store() -> UsageStore:
    """Usage database at BACKSTOP_USAGE_DB, default out/usage.db."""
    return UsageStore(Path(os.getenv("BACKSTOP_USAGE_DB", str(config.OUT_DIR / "usage.db"))))


@lru_cache(maxsize=1)
def get_rate_limiter() -> RateLimiter:
    """Per-key limit from BACKSTOP_RATE_LIMIT_PER_MIN, default 60 requests per minute."""
    return RateLimiter(limit=int(os.getenv("BACKSTOP_RATE_LIMIT_PER_MIN", "60")), window_s=60.0)


@lru_cache(maxsize=1)
def get_client() -> BackstopTelem:
    """Shared BackstopTelem for the process, configured from the environment."""
    return BackstopTelem()


def require_key(authorization: str | None = Header(default=None)) -> str:
    """Return the caller's key if the bearer token matches a configured key, else 401."""
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise _error(401, "unauthorized", "Send Authorization: Bearer <key>.", {"WWW-Authenticate": "Bearer"})
    if not any(hmac.compare_digest(token.encode(), key.encode()) for key in configured_keys()):
        raise _error(401, "unauthorized", "API key not recognized.", {"WWW-Authenticate": "Bearer"})
    return token


def rate_limited_key(
    api_key: str = Depends(require_key), limiter: RateLimiter = Depends(get_rate_limiter),
) -> str:
    """Enforce the per-key rate limit, returning 429 with Retry-After when exceeded."""
    allowed, retry_after = limiter.allow(api_key)
    if not allowed:
        raise _error(429, "rate_limited", f"Rate limit of {limiter.limit} requests per minute exceeded.",
                     {"Retry-After": str(max(1, math.ceil(retry_after)))})
    return api_key


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness check; needs no key."""
    return {"status": "ok", "version": VERSION}


@router.post("/v1/search", response_model=BackstopSearchResponse)
async def search(
    body: SearchRequest,
    api_key: str = Depends(rate_limited_key),
    client: BackstopTelem = Depends(get_client),
    usage: UsageStore = Depends(get_usage_store),
) -> BackstopSearchResponse:
    """Run one verified search and meter it against the caller's key.

    Limits are clamped to the server maxima and to what remains of the key's
    spend cap; the first search plus one check must fit, else 402.
    """
    if body.session is not None and not usage.owns_session(api_key, body.session):
        raise _error(404, "session_not_found", _SESSION_NOT_FOUND)
    max_rescues = min(config.MAX_RESCUES if body.max_rescues is None else body.max_rescues,
                      config.BACKSTOP_MAX_RESCUES)
    want = min(body.max_cost_usd or config.SEARCH_MAX_COST_USD, config.BACKSTOP_MAX_COST_USD)
    first_step = config.PRICE_USD.get(config.FIRST_ENGINE, config.DEFAULT_PRICE_USD) + config.BACKSTOP_CHECK_COST_USD
    reserved = usage.reserve(api_key, max(want, first_step), minimum=first_step,
                             cap=config.BACKSTOP_KEY_SPEND_CAP_USD)
    if not reserved:
        raise _spend_cap_error()
    try:
        try:
            response = await client.search(
                body.query, goal=body.goal, session=body.session, max_rescues=max_rescues,
                max_cost_usd=min(want, reserved), timeout_s=body.timeout_s,
                checker_timeout_s=body.checker_timeout_s,
            )
        except TelemUnavailable as exc:
            logger.warning("Telem unavailable for key %s: %s", key_id(api_key), exc)
            raise _error(502, "telem_unavailable", "Telem returned no usable response from any engine.") from exc
        except config.BudgetExceeded as exc:
            logger.warning("Spend budget exhausted for key %s", key_id(api_key))
            raise _error(503, "budget_exhausted", "This deployment's spend budget is exhausted.") from exc
        usage.record(api_key, status=response.status, engine_calls=response.engine_calls,
                     fetches=response.fetches, cost_usd=response.cost_usd)
    finally:
        usage.release(api_key, reserved)
    if response.session_id and not usage.bind_session(api_key, response.session_id):
        logger.warning("Telem returned a session owned by another key to key %s; not exposing it",
                       key_id(api_key))
        response = response.model_copy(update={"session_id": None})
    return response


@router.get("/v1/usage")
async def usage_totals(
    api_key: str = Depends(require_key), usage: UsageStore = Depends(get_usage_store),
) -> dict[str, Any]:
    """The caller's metered totals: verified searches by status, engine calls, fetches, cost."""
    return usage.totals(api_key)
