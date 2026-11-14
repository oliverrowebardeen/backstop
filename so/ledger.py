"""Telem ledger labels: name each Backstop step on the Telem call itself.

Every Telem search Backstop makes can carry Telem's `context` string and a
`metadata` dict saying which Backstop step it is (initial, rescue_1, rescue_2,
engine_retry, extract_recheck) and which checker verdict triggered it. Telem's
session console then shows the whole verification trajectory, grouped by the
one session each agent question reuses.

The labels are additive and fail open: they never enter cache keys, and if
Telem rejects a request carrying them, `call_with_ledger` retries once without
them and logs a warning.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")

BACKSTOP_VERSION = "0.1.0"
"""Version reported in ledger metadata; matches backstop.__version__."""

# HTTP statuses meaning Telem refused the request body, so the ledger fields may be the cause.
_REJECTED_STATUSES = frozenset({400, 422})


def ledger(step: str, verdict: str | None, engine: str) -> dict[str, Any]:
    """Telem `context` and `metadata` keyword arguments naming one Backstop step.

    Args:
        step: initial, rescue_1, rescue_2, engine_retry or extract_recheck.
        verdict: The checker verdict that triggered this step (None for the first search).
        engine: The engine pinned for the call, or "auto" for Telem's own routing.
    """
    label = verdict or "none"
    return {
        "context": f"backstop step={step} verdict={label}",
        "metadata": {"backstop_version": BACKSTOP_VERSION, "verdict": label,
                     "engine": engine, "rescue_step": step},
    }


async def call_with_ledger(send: Callable[[dict[str, Any]], Awaitable[T]], fields: dict[str, Any]) -> T:
    """Call Telem with the ledger fields; if Telem rejects the request, retry once without them.

    `send` receives the extra keyword arguments to pass to the SDK call. Errors
    unrelated to a rejected body, and any error on the retry, propagate unchanged.
    """
    extra = {key: value for key, value in fields.items() if value}
    try:
        return await send(extra)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if not extra or status not in _REJECTED_STATUSES:
            raise
        logger.warning("Telem rejected the request with ledger fields %s (HTTP %s); retrying without them",
                       ", ".join(sorted(extra)), status)
    return await send({})
