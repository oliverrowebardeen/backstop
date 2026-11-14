"""Append-only JSONL events shared by evaluations and the live race screen."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any

from so.contracts import Event, EventType, Setup


class EventLog:
    """Write each validated event immediately, with a monotonically increasing seq."""

    def __init__(self, run_id: str, question_id: str, setup: Setup, *,
                 log_dir: str | Path = "out/logs") -> None:
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ValueError("run_id must be a filename component")
        self.run_id, self.question_id, self.setup = run_id, question_id, setup
        self.path = Path(log_dir) / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_log(self.path) if self.path.exists() else []
        self._seq = max((row["seq"] for row in existing), default=0)
        self._lock = threading.Lock()

    def emit(self, type: EventType, **data: Any) -> Event:
        """Append and flush an event; fetch events must specify their via provider."""
        with self._lock:
            event = Event(ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                          run_id=self.run_id, question_id=self.question_id, setup=self.setup,
                          seq=self._seq + 1, type=type, data=data)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(event.model_dump_json() + "\n")
            self._seq = event.seq
            return event


def read_log(path: str | Path) -> list[dict[str, Any]]:
    """Read validated wire dictionaries, ignoring only an unfinished trailing line."""
    text = Path(path).read_text(encoding="utf-8")
    rows = []
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not line.endswith("\n"):
                break
            raise
        rows.append(Event.model_validate(value).model_dump(mode="json"))
    return rows


def summarize(path: str | Path) -> dict[str, int | float]:
    """Return run_end totals, or derive totals for an incomplete run without double counting."""
    events = read_log(path)
    for event in reversed(events):
        if event["type"] == "run_end":
            return dict(event["data"])
    totals: dict[str, int | float] = dict(engine_calls=0, fetches=0, searches=0,
        cost_usd_search=0.0, cost_usd_llm=0.0, cost_usd_total=0.0, latency_ms=0.0)
    for event in events:
        kind, data = event["type"], event["data"]
        if kind == "search":
            totals["engine_calls"] += len(data.get("engines", []))
            totals["cost_usd_search"] += data.get("cost_usd", 0)
        elif kind == "fetch":
            totals["fetches"] += 1
            totals["cost_usd_search"] += data.get("cost_usd", 0)
        elif kind == "llm" and data.get("role") != "judge":
            totals["cost_usd_llm"] += data.get("cost_usd", 0)
        elif kind == "search_done":
            totals["searches"] += 1
    totals["cost_usd_total"] = totals["cost_usd_search"] + totals["cost_usd_llm"]
    if events:
        totals["latency_ms"] = max(0, (datetime.fromisoformat(events[-1]["ts"]) -
                                       datetime.fromisoformat(events[0]["ts"])).total_seconds() * 1000)
    return totals
