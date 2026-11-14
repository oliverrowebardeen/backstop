"""Telem Monitors through the telem SDK: create, poll, update, delete, and webhook checks.

Every SDK failure becomes a `MonitorsError` that carries the HTTP status and body, and is
logged with both before it is raised, so callers can log-and-continue without losing detail.
"""
from __future__ import annotations

import hashlib
import logging
import warnings
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: How long a snippet of a received item is kept.
SNIPPET_CHARS = 600


class MonitorsError(RuntimeError):
    """A Telem Monitors call failed.

    Attributes:
        op: The operation (create, poll, update, delete).
        status: The HTTP status, or None when no response arrived.
        body: The response body, or the error text.
    """

    def __init__(self, op: str, status: int | None, body: Any, message: str = "") -> None:
        self.op, self.status, self.body = op, status, body
        super().__init__(message or f"Telem Monitors {op} failed: status={status} body={body!r}")


@dataclass(frozen=True)
class MonitorItem:
    """One result a monitor delivered, by poll or webhook."""

    url: str
    title: str
    snippet: str
    published: str | None
    provider: str

    @property
    def key(self) -> str:
        """Stable identity: the URL, or a hash of the text when there is no URL."""
        if self.url:
            return self.url
        return "sha1:" + hashlib.sha1(f"{self.title}\n{self.snippet}".encode()).hexdigest()


@dataclass(frozen=True)
class CreatedMonitor:
    """What a create returned that the fact sheet keeps."""

    id: str
    webhook_secret: str | None
    session_id: str | None


class MonitorsAPI(Protocol):
    """The slice of ``telem.AsyncTelem().monitors`` this package uses."""

    async def create(self, query: str, **kwargs: Any) -> Any: ...
    async def poll(self, monitor_id: str, **kwargs: Any) -> Any: ...
    async def update(self, monitor_id: str, **kwargs: Any) -> Any: ...
    async def delete(self, monitor_id: str) -> None: ...


def item_from_result(result: Any) -> MonitorItem:
    """Normalize a telem ``SearchResult`` or a plain dict into a `MonitorItem`."""
    get = result.get if isinstance(result, dict) else (lambda name, default=None: getattr(result, name, default))
    excerpt = get("excerpt") or []
    if isinstance(excerpt, str):
        excerpt = [excerpt]
    text = get("summary") or get("snippet") or get("content") or " ".join(str(part) for part in excerpt)
    return MonitorItem(
        url=str(get("url") or ""), title=str(get("title") or ""),
        snippet=" ".join(str(text or "").split())[:SNIPPET_CHARS],
        published=get("publish_date") or get("published") or None,
        provider=str(get("provider") or "telem_monitor"),
    )


def items_from_payload(payload: Any) -> list[MonitorItem]:
    """Every result in a poll response or webhook body, whatever envelope it came in.

    Accepts a telem ``SearchResponse``, a router interaction (``preprocessor_runs``), or a
    dict with ``results``/``items`` (top level or under ``data``).
    """
    results = getattr(payload, "results", None)
    if results is not None and not isinstance(payload, dict):
        return [item_from_result(result) for result in results]
    if not isinstance(payload, dict):
        return []
    if payload.get("preprocessor_runs"):
        from telem._normalize import build_search_response

        return [item_from_result(result) for result in build_search_response(payload).results]
    for key in ("results", "items", "new_results"):
        if isinstance(payload.get(key), list):
            return [item_from_result(row) for row in payload[key] if isinstance(row, dict)]
    if isinstance(payload.get("data"), dict):
        return items_from_payload(payload["data"])
    return []


def verify_signature(body: bytes | str, header: str | None, secret: str | None) -> bool:
    """Check a ``Telem-Signature`` header with the SDK's ``verify_webhook``; False on any doubt."""
    if not header or not secret:
        return False
    from telem import verify_webhook

    try:
        return bool(verify_webhook(body, header, secret))
    except (ValueError, TypeError):
        return False


class MonitorsGateway:
    """Telem Monitors calls with uniform error handling.

    Args:
        api: An object with the SDK's async monitors methods; defaults to
            ``telem.AsyncTelem(base_url=TELEM_BASE_URL).monitors``.
    """

    def __init__(self, api: MonitorsAPI | None = None) -> None:
        self._owned: Any = None
        if api is None:
            from telem import AsyncTelem

            from so import config

            self._owned = AsyncTelem(base_url=config.TELEM_BASE_URL, timeout=config.TELEM_TIMEOUT_S)
            api = self._owned.monitors
        self.api = api

    async def aclose(self) -> None:
        """Close the Telem client this gateway built, if any."""
        if self._owned is not None:
            await self._owned.aclose()

    async def create(self, description: str, *, frequency: str, include_domains: list[str] | None = None,
                     webhook_url: str | None = None) -> CreatedMonitor:
        """Create a monitor without the monitor agent, read by poll or webhook.

        Raises:
            MonitorsError: The create failed.
        """
        kwargs: dict[str, Any] = {"frequency": frequency, "agent": False}
        if include_domains:
            kwargs["include_domains"] = list(include_domains)
        if webhook_url:
            kwargs["webhook_url"] = webhook_url
        monitor = await self._call("create", self.api.create(description, **kwargs))
        return CreatedMonitor(id=str(monitor.id), webhook_secret=getattr(monitor, "webhook_secret", None),
                              session_id=getattr(monitor, "session_id", None))

    async def poll(self, monitor_id: str) -> list[MonitorItem]:
        """The monitor's results that are new since the last poll; each is returned once.

        Raises:
            MonitorsError: The poll failed.
        """
        with warnings.catch_warnings():
            # poll() warns that the agent's changes feed is preferred; this package reads
            # monitors without the agent, where poll is the read.
            warnings.simplefilter("ignore", DeprecationWarning)
            response = await self._call("poll", self.api.poll(monitor_id))
        return items_from_payload(response)

    async def update(self, monitor_id: str, **fields: Any) -> Any:
        """PATCH a monitor (status, frequency, webhook_url, ttl).

        Raises:
            MonitorsError: The update failed.
        """
        return await self._call("update", self.api.update(monitor_id, **fields))

    async def delete(self, monitor_id: str) -> None:
        """Delete a monitor.

        Raises:
            MonitorsError: The delete failed.
        """
        await self._call("delete", self.api.delete(monitor_id))

    async def _call(self, op: str, call: Any) -> Any:
        try:
            return await call
        except MonitorsError:
            raise
        except ValueError as exc:
            logger.error("Telem Monitors %s refused before sending: %s", op, exc)
            raise MonitorsError(op, None, str(exc)) from exc
        except Exception as exc:  # the SDK's TelemError family, httpx errors, anything else
            status = getattr(exc, "status_code", None)
            body = getattr(exc, "body", None)
            if body is None:
                body = f"{type(exc).__name__}: {exc}"
            logger.error("Telem Monitors %s failed: status=%s body=%s", op, status, body)
            raise MonitorsError(op, status, body) from exc
