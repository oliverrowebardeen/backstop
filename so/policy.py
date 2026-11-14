"""Fixed rescue actions, with engine ordering learned outside the policy."""

from __future__ import annotations

import calendar
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from so.contracts import CheckerVerdict, RescueTable

MAX_RESCUES = 2
RESCUE_TABLE_PATH = Path(__file__).resolve().parents[1] / "out" / "rescue_table.json"


@dataclass(frozen=True)
class Action:
    kind: Literal["HIT", "NOT_FOUND", "RETRY_ENGINE", "FETCH_PAGE"]
    engine: str | None = None
    query: str | None = None
    url: str | None = None
    fresh: bool = False


def load_rescue_table(path: str | Path | None = None) -> RescueTable:
    """Read current learn-split rankings, or use configured engine ordering."""
    from so.config import fallback_order

    target = Path(path) if path is not None else RESCUE_TABLE_PATH
    try:
        return RescueTable.model_validate_json(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RescueTable(fallback_order=list(fallback_order))


def next_action(
    verdict: CheckerVerdict,
    tried_engines: Sequence[str],
    rescues_used: int,
    table: RescueTable,
) -> Action:
    """Choose one action; tried engines must be in their attempted order."""
    if verdict.verdict == "HIT":
        return Action("HIT")
    if rescues_used >= MAX_RESCUES:
        return Action("NOT_FOUND")
    if verdict.verdict == "SHALLOW" and verdict.best_url:
        return Action("FETCH_PAGE", url=verdict.best_url)

    previous = tried_engines[-1] if tried_engines else ""
    candidates = [*table.order_after.get(previous, []), *table.fallback_order]
    engine = next((item for item in candidates if item not in tried_engines), None)
    if engine is None:
        return Action("NOT_FOUND")
    return Action(
        "RETRY_ENGINE",
        engine=engine,
        query=verdict.rewrite or None,
        fresh=verdict.verdict == "STALE",
    )


def freshness_cutoff(goal: str, today: date) -> str:
    """Use a stated date or period start, otherwise a rolling 90-day window."""
    dates: list[date] = []
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"\b((?:19|20)\d{2})[-/](\d{1,2})(?:[-/](\d{1,2}))?\b", goal):
        spans.append(match.span())
        try:
            dates.append(date(int(match[1]), int(match[2]), int(match[3] or 1)))
        except ValueError:
            pass

    months = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
    months.update({name.lower(): number for number, name in enumerate(calendar.month_abbr) if name})
    month_names = "|".join(months)
    patterns = (
        rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{month_names})\.?\s+(?P<year>(?:19|20)\d{{2}})\b",
        rf"\b(?P<month>{month_names})\.?\s+(?:(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s+)?(?P<year>(?:19|20)\d{{2}})\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, goal, re.IGNORECASE):
            if any(start <= match.start() < end for start, end in spans):
                continue
            spans.append(match.span())
            try:
                dates.append(date(int(match["year"]), months[match["month"].lower()], int(match["day"] or 1)))
            except ValueError:
                pass
    for match in re.finditer(r"\b(?:19|20)\d{2}\b", goal):
        if not any(start <= match.start() < end for start, end in spans):
            dates.append(date(int(match[0]), 1, 1))
    return (min(dates) if dates else today - timedelta(days=90)).isoformat()
