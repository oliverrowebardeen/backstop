"""Per-key usage metering in SQLite and a per-key sliding-window rate limit.

Keys are never stored: rows are keyed by `key_id`, a SHA-256 prefix of the key.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    key_id TEXT PRIMARY KEY,
    verified_searches INTEGER NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    not_found INTEGER NOT NULL DEFAULT 0,
    unchecked INTEGER NOT NULL DEFAULT 0,
    engine_calls INTEGER NOT NULL DEFAULT 0,
    fetches INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
)
"""

_STATUS_COLUMN = {"HIT": "hits", "NOT_FOUND": "not_found", "UNCHECKED": "unchecked"}


def key_id(api_key: str) -> str:
    """Stable, non-reversible identifier for an API key, safe to store and log."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


class UsageStore:
    """Accumulate verified searches, engine calls, fetches and cost per key."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, key: str, *, status: str, engine_calls: int, fetches: int, cost_usd: float) -> None:
        """Add one verified search to the caller's totals."""
        column = _STATUS_COLUMN.get(status)
        if column is None:
            raise ValueError(f"Unknown search status {status!r}")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connect() as db:
            db.execute(
                f"""INSERT INTO usage (key_id, verified_searches, {column}, engine_calls, fetches, cost_usd,
                                       first_seen, last_seen)
                    VALUES (?, 1, 1, ?, ?, ?, ?, ?)
                    ON CONFLICT(key_id) DO UPDATE SET
                        verified_searches = verified_searches + 1,
                        {column} = {column} + 1,
                        engine_calls = engine_calls + excluded.engine_calls,
                        fetches = fetches + excluded.fetches,
                        cost_usd = cost_usd + excluded.cost_usd,
                        last_seen = excluded.last_seen""",
                (key_id(key), engine_calls, fetches, cost_usd, now, now),
            )

    def totals(self, key: str) -> dict[str, Any]:
        """Return the caller's totals; zeros if the key has not searched yet."""
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM usage WHERE key_id = ?", (key_id(key),)).fetchone()
        if row is None:
            return {"key_id": key_id(key), "verified_searches": 0, "hits": 0, "not_found": 0, "unchecked": 0,
                    "engine_calls": 0, "fetches": 0, "cost_usd": 0.0, "first_seen": None, "last_seen": None}
        totals = dict(row)
        totals["cost_usd"] = round(totals["cost_usd"], 6)
        return totals


class RateLimiter:
    """Allow at most `limit` requests per key in any rolling `window_s` seconds."""

    def __init__(self, limit: int = 60, window_s: float = 60.0) -> None:
        if limit < 1 or window_s <= 0:
            raise ValueError("Rate limit and window must be positive")
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """Record a request if allowed. Returns (allowed, seconds until the next slot)."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key_id(key), deque())
            while hits and moment - hits[0] >= self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, max(0.0, self.window_s - (moment - hits[0]))
            hits.append(moment)
            return True, 0.0
