"""Find evaluation runs that demonstrate each demo story.

  python -m demo.candidates                 # list candidates per story
  python -m demo.candidates --write         # also write the top pick per story to demo/golden.json
  python -m demo.candidates --log-dir DIR   # read eval logs from DIR instead of SO_LOG_DIR

Reads finished evaluation logs for test questions and traps, grades each final
answer with the evaluation judge (the same cached grading `evals.score` uses, so
no number changes), and applies the rules in `demo.stories`. Candidates are
ranked by Second Opinion wall time, so the shortest clear run comes first.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data.load import load_questions, load_traps
from demo.stories import STORIES, Grade, RunView, Story, search_path, stories_for
from so import config, eventlog
from so.contracts import Question

BASELINE = "single"
OURS = "second_opinion"
GOLDEN_PATH = Path(config.SERVER_DEMO_DIR) / "golden.json"
Grader = Callable[[Question, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class Candidate:
    """A question whose graded baseline and Second Opinion runs show one story."""

    story: Story
    question_id: str
    question: str
    baseline: dict[str, Any]
    ours: dict[str, Any]


def _side(view: RunView, *, path: bool = False) -> dict[str, Any]:
    side = {"setup": view.setup, "grade": view.grade, "status": view.status, "final": view.final,
            "engine_calls": view.totals.get("engine_calls"), "cost_usd": view.totals.get("cost_usd_total"),
            "latency_ms": view.totals.get("latency_ms")}
    if path:
        side["path"] = search_path(view.events)
    return side


def eval_questions() -> dict[str, Question]:
    """Only reported questions qualify: the test split and the traps."""
    questions = load_questions(split="test") + [t for t in load_traps() if t.split == "test"]
    return {q.id: q for q in questions}


async def _default_grade(question: Question, answer: dict[str, Any]) -> str:
    from evals.judge import grade_answer

    return await grade_answer(question, answer)


async def find_candidates(log_dir: Path, grade: Grader = _default_grade) -> dict[Story, list[Candidate]]:
    """Grade every finished baseline and Second Opinion pair and group them by story."""
    from evals.run_eval import finished_runs

    questions = eval_questions()
    runs = finished_runs(log_dir)
    pairs = [qid for qid in sorted(questions) if (qid, BASELINE) in runs and (qid, OURS) in runs]
    limit = asyncio.Semaphore(config.LLM_MAX_CONCURRENCY)

    async def view(qid: str, setup: str) -> RunView:
        events = eventlog.read_log(runs[(qid, setup)])
        unscored = RunView.from_events(setup, events)
        async with limit:
            graded = await grade(questions[qid], unscored.answer or {"final": "", "status": "NOT_FOUND"})
        return RunView.from_events(setup, events, grade=_grade(graded))

    views = await asyncio.gather(*(asyncio.gather(view(q, BASELINE), view(q, OURS)) for q in pairs))
    found: dict[Story, list[Candidate]] = {story: [] for story in STORIES}
    for qid, (baseline, ours) in zip(pairs, views):
        for story in stories_for(questions[qid], baseline, ours):
            found[story].append(Candidate(story, qid, questions[qid].question,
                                          _side(baseline), _side(ours, path=True)))
    for items in found.values():
        items.sort(key=lambda c: (c.ours["latency_ms"] or float("inf"), c.question_id))
    return found


def _grade(value: str) -> Grade:
    if value not in ("correct", "wrong", "declined"):
        raise ValueError(f"Unexpected judge grade: {value!r}")
    return value  # type: ignore[return-value]


def golden_entries(found: dict[Story, list[Candidate]]) -> list[dict[str, Any]]:
    """The top candidate per story, in the form `demo.record --golden` reads."""
    entries = []
    for story in STORIES:
        if not found[story]:
            continue
        top = found[story][0]
        entries.append({"name": story.replace("_", "-"), "story": story, "question_id": top.question_id,
                        "question": top.question, "left": BASELINE, "right": OURS,
                        "eval_evidence": {"baseline": top.baseline, "second_opinion": top.ours}})
    return entries


def write_golden(entries: list[dict[str, Any]], path: Path = GOLDEN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "source": "finished evaluation logs on the test split and traps, graded by the evaluation judge",
               "golden": entries}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _print(found: dict[Story, list[Candidate]], limit: int) -> None:
    for story in STORIES:
        print(f"\n{story}: {len(found[story])} candidate(s)")
        for candidate in found[story][:limit]:
            ours, base = candidate.ours, candidate.baseline
            print(f"  {candidate.question_id}  {candidate.question[:100]}")
            print(f"    {base['setup']}: {base['grade']} | {base['final'][:120]}")
            print(f"    {ours['setup']}: {ours['grade']} | {ours['final'][:120]}")
            for line in ours["path"]:
                print(f"      search: {line}")
            print(f"    second_opinion cost ${ours['cost_usd'] or 0:.4f}, {(ours['latency_ms'] or 0) / 1000:.1f}s")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log-dir", type=Path, default=Path(config.SO_LOG_DIR),
                        help="directory of finished eval logs (default: SO_LOG_DIR, where evals.run_eval writes)")
    parser.add_argument("--limit", type=int, default=5, help="candidates printed per story")
    parser.add_argument("--write", action="store_true", help=f"write the top pick per story to {GOLDEN_PATH.name}")
    args = parser.parse_args(argv)
    found = asyncio.run(find_candidates(args.log_dir))
    _print(found, args.limit)
    if args.write:
        entries = golden_entries(found)
        write_golden(entries)
        print(f"\nwrote {len(entries)} golden question(s) to {GOLDEN_PATH}")
        if len(entries) < len(STORIES):
            missing = sorted(set(STORIES) - {entry["story"] for entry in entries})
            print(f"no candidate for: {', '.join(missing)}")


if __name__ == "__main__":
    main()
