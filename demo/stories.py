"""Rules that decide whether a graded pair of runs demonstrates one of the demo stories.

A story compares a baseline run (normally `single`) with a Second Opinion run of
the same question:

- `retry_recovery`: the baseline answers and is graded wrong; Second Opinion is
  graded correct after a counted engine retry that ended in a HIT.
- `trap_decline`: on a false-premise question the baseline answers and is graded
  wrong; Second Opinion answers NOT FOUND.
- `shallow_fetch`: Second Opinion is graded correct after a counted page read
  that ended in a HIT.

Grades come from the evaluation judge. These rules never see gold answers.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from so.contracts import Question

Story = Literal["retry_recovery", "trap_decline", "shallow_fetch"]
STORIES: tuple[Story, ...] = ("retry_recovery", "trap_decline", "shallow_fetch")
Grade = Literal["correct", "wrong", "declined"]


@dataclass(frozen=True)
class RunView:
    """One finished run: its events, its final answer and the judge's grade."""

    setup: str
    events: Sequence[dict[str, Any]]
    grade: Grade | None = None
    answer: dict[str, Any] | None = None
    totals: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_events(cls, setup: str, events: Sequence[dict[str, Any]], grade: Grade | None = None) -> RunView:
        answer = next((e["data"] for e in events if e["type"] == "answer"), None)
        totals = next((e["data"] for e in reversed(events) if e["type"] == "run_end"), {})
        return cls(setup=setup, events=events, grade=grade, answer=answer, totals=totals)

    @property
    def status(self) -> str | None:
        return (self.answer or {}).get("status")

    @property
    def final(self) -> str:
        return str((self.answer or {}).get("final", ""))


def rescued_by(events: Sequence[dict[str, Any]], action: str) -> bool:
    """True when a counted rescue of this kind is followed by a HIT in the same search call.

    Engine-error retries carry `counted: False` and are not misses, so they do not count.
    """
    pending = False
    for event in events:
        kind, data = event["type"], event["data"]
        if kind == "rescue" and data.get("action") == action and data.get("counted", True):
            pending = True
        elif kind == "check" and pending and data.get("verdict") == "HIT":
            return True
        elif kind == "search_done":
            pending = False
    return False


def search_path(events: Sequence[dict[str, Any]]) -> list[str]:
    """Each search tool call as one line, for example `tavily DEAD > exa HIT = HIT`."""
    calls: list[str] = []
    steps: list[str] = []
    for event in events:
        kind, data = event["type"], event["data"]
        if kind == "search":
            steps.append(",".join(data.get("engines", [])))
        elif kind == "fetch":
            steps.append(f"read via {data.get('via')}" + ("" if data.get("ok") else " (failed)"))
        elif kind == "check" and steps:
            steps[-1] += f" {data.get('verdict')}"
        elif kind == "search_done":
            calls.append(" > ".join(steps) + f" = {data.get('status')}")
            steps = []
    return calls


def stories_for(question: Question, baseline: RunView, ours: RunView) -> list[Story]:
    """The stories this pair demonstrates. Traps only qualify for `trap_decline`."""
    confident_wrong = baseline.grade == "wrong" and baseline.status == "ANSWERED"
    if question.is_trap:
        return ["trap_decline"] if confident_wrong and ours.status == "NOT_FOUND" else []
    found: list[Story] = []
    if confident_wrong and ours.grade == "correct" and rescued_by(ours.events, "RETRY_ENGINE"):
        found.append("retry_recovery")
    if ours.grade == "correct" and rescued_by(ours.events, "FETCH_PAGE"):
        found.append("shallow_fetch")
    return found
