"""Fake so.eventlog: read_log(path) and summarize(path) over section 6.11 logs."""
from __future__ import annotations

import json
from pathlib import Path


def read_log(path) -> list[dict]:
    """Every complete event in the file, in order. A torn last line is skipped."""
    events = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def summarize(path) -> dict | None:
    """The run_end totals of a finished run, or None if the run never finished."""
    for ev in reversed(read_log(path)):
        if ev.get("type") == "run_end":
            return dict(ev.get("data") or {})
    return None
