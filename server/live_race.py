"""Public live-race mode: explicit opt-in, demo code, per-IP and server-wide limits, cost cap and recordings.

BACKSTOP_LIVE_RACE=true turns on live races for anyone who can open the page (no API key in the
browser). Each race is guarded: an optional BACKSTOP_DEMO_CODE, at most BACKSTOP_MAX_RACES races at
once, BACKSTOP_RACE_RATE_PER_HOUR races per IP, BACKSTOP_RACE_TIMEOUT_S seconds and
BACKSTOP_RACE_MAX_USD of recorded spend per race. When the variable is unset, the older
SERVER_LIVE_RACES switch applies with hosted API key authentication, exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import json
import math
import os
from pathlib import Path
import re
from typing import Any

from fastapi import HTTPException, Request

from server.errors import RunFailure
from server.usage import RateLimiter
from so import config

PUBLIC_MAX_QUESTION_CHARS = 300
MAX_SAVED_RECORDINGS = 50
TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LiveRaceEnv:
    """What the environment asks for. `explicit` is None when BACKSTOP_LIVE_RACE is unset."""
    explicit: bool | None
    max_usd: float
    timeout_s: float
    max_races: int
    rate_per_hour: int
    demo_code: str | None

    @classmethod
    def read(cls) -> LiveRaceEnv:
        raw = os.getenv("BACKSTOP_LIVE_RACE")
        explicit = None if raw is None or not raw.strip() else raw.strip().lower() in TRUE
        values = cls(explicit=explicit,
                     max_usd=float(os.getenv("BACKSTOP_RACE_MAX_USD", "0.50")),
                     timeout_s=float(os.getenv("BACKSTOP_RACE_TIMEOUT_S", "90")),
                     max_races=int(os.getenv("BACKSTOP_MAX_RACES", "2")),
                     rate_per_hour=int(os.getenv("BACKSTOP_RACE_RATE_PER_HOUR", "10")),
                     demo_code=os.getenv("BACKSTOP_DEMO_CODE", "").strip() or None)
        if not (values.max_usd > 0 and values.timeout_s > 0 and values.max_races >= 1 and values.rate_per_hour >= 1):
            raise ValueError("BACKSTOP_RACE_MAX_USD, BACKSTOP_RACE_TIMEOUT_S, BACKSTOP_MAX_RACES and "
                             "BACKSTOP_RACE_RATE_PER_HOUR must be positive")
        return values


class RaceStopped(Exception):
    """A guard stopped the race. `failure` is the plain message shown in the column."""

    def __init__(self, failure: RunFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def cost_cap_failure(spent: float, cap: float) -> RunFailure:
    return RunFailure(error_type="RaceCostCap",
                      message=f"Stopped at cost cap: this race spent ${spent:.4f} of its ${cap:.2f} limit.")


def time_limit_failure(seconds: float) -> RunFailure:
    return RunFailure(error_type="RaceTimeLimit",
                      message=f"Stopped at the {seconds:g} second time limit for a live race.")


def _http(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message}, headers=headers)


class DemoGuard:
    """Admission for public live races. Checks run cheapest first; a busy server does not use quota."""

    def __init__(self, *, demo_code: str | None, max_races: int, rate_per_hour: int) -> None:
        self.demo_code = demo_code
        self.max_races = max_races
        self.limiter = RateLimiter(limit=rate_per_hour, window_s=3600.0)

    def check_code(self, supplied: str | None) -> None:
        if self.demo_code and not hmac.compare_digest((supplied or "").strip().encode(), self.demo_code.encode()):
            raise _http(401, "demo_code_required", "Enter the demo code to run a live race.")

    def admit(self, request: Request, supplied_code: str | None, active_runs: int) -> str:
        self.check_code(supplied_code)
        if active_runs + 2 > 2 * self.max_races:
            raise _http(429, "busy", "Busy, try again in a moment.", {"Retry-After": "15"})
        address = client_address(request)
        allowed, retry_after = self.limiter.allow(f"race-ip:{address}")
        if not allowed:
            raise _http(429, "rate_limited",
                        f"This address has started {self.limiter.limit} live races in the last hour. "
                        "Watch a recorded run, or try again later.",
                        {"Retry-After": str(max(1, math.ceil(retry_after)))})
        return f"demo:{address}"


def client_address(request: Request) -> str:
    """The caller's IP. On Vercel the platform sets x-vercel-forwarded-for and overwrites
    x-forwarded-for, so clients cannot spoof them; elsewhere only the socket address is trusted."""
    if os.getenv("VERCEL"):
        for header in ("x-vercel-forwarded-for", "x-forwarded-for", "x-real-ip"):
            value = request.headers.get(header, "").split(",")[0].strip()
            if value:
                return value
    return request.client.host if request.client else "unknown"


def missing_live_keys() -> tuple[str, ...]:
    """Provider keys a live race needs with the current settings that are not set."""
    names = ["TELEM_API_KEY", "TAVILY_API_KEY", f"{config.LLM_PROVIDER.upper()}_API_KEY"]
    if config.CHECKER_BACKEND != "llm":
        names.append("OPENAI_API_KEY")  # the cascade's first stage is the OpenAI Decisions model
    if config.CHECKER_BACKEND != "decision":
        names.append(f"{config.CHECKER_PROVIDER.upper()}_API_KEY")
    return tuple(name for name in dict.fromkeys(names) if not os.getenv(name, "").strip())


def live_off_reason(enabled: bool, missing: tuple[str, ...]) -> str | None:
    """One plain line for the race section when live races cannot run here."""
    if not enabled:
        return "Live races are off on this server (BACKSTOP_LIVE_RACE is not true), so this section plays recorded races."
    if missing:
        names = ", ".join(missing[:-1]) + (" and " if len(missing) > 1 else "") + missing[-1]
        verb = "is" if len(missing) == 1 else "are"
        return f"Live races are off on this server because {names} {verb} not set, so this section plays recorded races."
    return None


SAFE_RUN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def find_log(log_dir: Path, run_id: str) -> Path | None:
    if not SAFE_RUN.fullmatch(run_id):
        return None
    return next(log_dir.rglob(f"{run_id}.jsonl"), None)


def save_recording(log_dir: Path, demo_dir: Path, left_run_id: str, right_run_id: str,
                   validate: Any) -> dict[str, str]:
    """Copy two finished live runs into the replay format: a manifest plus left/right JSONL logs."""
    paths = [find_log(log_dir, run_id) for run_id in (left_run_id, right_run_id)]
    if any(path is None or not path.is_file() for path in paths):
        raise _http(404, "run_not_found", "Both runs must exist on this server to save a recording.")
    left, right = (validate(path) for path in paths)  # raises ValueError for unfinished or invalid logs
    demo_dir.mkdir(parents=True, exist_ok=True)
    if len(list(demo_dir.glob("live-*.json"))) >= MAX_SAVED_RECORDINGS:
        raise _http(409, "recordings_full", "The recording folder is full. Remove older live recordings first.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"live-{stamp}-{left_run_id[-6:]}"
    folder = demo_dir / name
    folder.mkdir()
    for side, path in (("left", paths[0]), ("right", paths[1])):
        (folder / f"{side}.jsonl").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    question = left[0].data.get("question", "Live race")
    manifest = {"title": question if len(question) <= 90 else f"{question[:87]}...",
                "left": f"{name}/left.jsonl", "right": f"{name}/right.jsonl",
                "recorded": "live race saved from the race screen"}
    (demo_dir / f"{name}.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    del right
    return {"name": name}


def golden_examples(demo_dir: Path, limit: int = 6) -> list[str]:
    """Example questions from demo/golden.json when it exists; the UI has its own fallback list."""
    try:
        data = json.loads((demo_dir / "golden.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data if isinstance(data, list) else data.get("questions", []) if isinstance(data, dict) else []
    questions = []
    for item in items:
        text = item.get("question") if isinstance(item, dict) else item if isinstance(item, str) else None
        if isinstance(text, str) and text.strip() and len(text.strip()) <= PUBLIC_MAX_QUESTION_CHARS:
            questions.append(text.strip())
    return list(dict.fromkeys(questions))[:limit]
