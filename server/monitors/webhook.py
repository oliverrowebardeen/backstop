"""Webhook receiver for Telem Monitors deliveries.

Each monitor is created with ``webhook_url = {base}/monitors/webhook/{field_id}``. A
delivery is accepted only when its ``Telem-Signature`` header verifies, with the SDK's
``verify_webhook``, against the secret Telem returned when that monitor was created. The
items in a verified delivery go through the same store-and-check path as a poll.

Run it on its own with ``uvicorn server.monitors.webhook:app``, or mount ``router`` in
another FastAPI app after calling ``configure``.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request

from server.monitors.gateway import items_from_payload, verify_signature
from server.monitors.runner import FactSheetRunner
from server.monitors.store import FactSheetStore

logger = logging.getLogger(__name__)

router = APIRouter()
_state: dict[str, FactSheetRunner] = {}


def configure(runner: FactSheetRunner) -> None:
    """Set the runner the webhook route stores and checks deliveries with."""
    _state["runner"] = runner


def _runner() -> FactSheetRunner:
    if "runner" not in _state:
        configure(FactSheetRunner(FactSheetStore()))
    return _state["runner"]


@router.post("/monitors/webhook/{field_id}")
async def receive(field_id: int, request: Request) -> dict[str, Any]:
    """Verify one delivery and ingest its items. 401 on a bad signature, 404 on an unknown field."""
    runner = _runner()
    row = runner.store.get_field(field_id)
    if row is None or not row.get("webhook_secret"):
        raise HTTPException(status_code=404, detail="unknown monitor field")
    body = await request.body()
    if not verify_signature(body, request.headers.get("Telem-Signature"), row["webhook_secret"]):
        logger.warning("Rejected a monitor webhook for field %s: bad or missing Telem-Signature", field_id)
        raise HTTPException(status_code=401, detail="invalid Telem-Signature")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="body is not JSON") from exc
    new, stale = await runner.ingest(field_id, items_from_payload(payload), via="webhook")
    return {"field_id": field_id, "new_items": new, "stale": stale}


def create_app(runner: FactSheetRunner | None = None) -> FastAPI:
    """A small FastAPI app serving only the webhook route."""
    if runner is not None:
        configure(runner)
    app = FastAPI(title="Backstop fact sheet monitors")
    app.include_router(router)
    return app


app = create_app()
