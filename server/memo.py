"""Check a memo on the product page: the public demo route and the recorded examples.

POST /memo-check is the browser's route. In public live mode (BACKSTOP_LIVE_RACE=true)
it is admitted by the live race's own DemoGuard from server/live_race.py: the same demo
code, the same per-IP hourly limit (a memo check counts as one race) and the same
capacity (a running memo check holds one race slot). Otherwise it needs the hosted API
key and is metered like POST /v1/memo-check. Live checks skip the disk cache, as live
races do. GET /memo/recordings lists finished reports saved in demo/memo_examples/ for
the replay fallback.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import ValidationError

from server import api_v1
from server.live_race import DemoGuard
from server.usage import RateLimiter, UsageStore
from so import claims, config

SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _text(body: dict[str, Any]) -> str:
    try:
        return api_v1.MemoCheckRequest.model_validate(body).text
    except ValidationError:
        raise HTTPException(422, {"code": "invalid_memo",
                                  "message": f"Paste a memo of 1 to {claims.MAX_MEMO_CHARS:,} characters."}) from None


def install_memo(app: FastAPI, settings: Any, guard: DemoGuard, active_races: Callable[[], int]) -> None:
    public = settings.race_access == "demo"
    examples_dir = Path(settings.demo_dir) / "memo_examples"
    running = {"memos": 0}

    @app.get("/memo/recordings")
    async def memo_recordings() -> list[dict[str, Any]]:
        out = []
        for path in sorted(examples_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"name": path.stem, "label": (data.get("recorded") or {}).get("label", path.stem),
                        "memo_id": (data.get("recorded") or {}).get("memo_id")})
        return out

    @app.get("/memo/recordings/{name}")
    async def memo_recording(name: str) -> dict[str, Any]:
        path = examples_dir / f"{name}.json"
        if not SAFE.fullmatch(name) or not path.is_file():
            raise HTTPException(404, "Recorded memo check not found.")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.post("/memo-check", response_model=claims.MemoReport)
    async def demo_memo_check(request: Request, body: dict[str, Any],
                              authorization: str | None = Header(default=None),
                              demo_code: str | None = Header(default=None, alias="X-Backstop-Demo-Code"),
                              limiter: RateLimiter = Depends(api_v1.get_rate_limiter),
                              usage: UsageStore = Depends(api_v1.get_usage_store)) -> claims.MemoReport:
        if not settings.live_races:
            raise HTTPException(403, {"code": "live_disabled",
                                      "message": "Live checks are off on this server. Showing a recorded check instead."})
        if public:
            guard.check_code(demo_code)
            text = _text(body)
            # A running memo check holds one race slot; it counts once against the per-IP race limit.
            guard.admit(request, demo_code, active_races() + 2 * running["memos"])
            running["memos"] += 1
            try:
                with config.bypass_cache():
                    return await claims.check_memo(text)
            finally:
                running["memos"] -= 1
        key = api_v1.rate_limited_key(api_v1.require_key(authorization), limiter)
        text = _text(body)
        with config.bypass_cache():
            return await api_v1.metered_memo_check(key, text, usage)
