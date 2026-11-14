"""Record a live race between two setups as a replay pair for the race screen.

  python -m demo.record --golden                       # every entry in demo/golden.json
  python -m demo.record --name NAME --question-id ID [--left single] [--right second_opinion]

Both setups run at the same time on the same question with the response cache
off, so every search, page read and model call in the recording is live and its
timing is real. Each pair is written as `demo/<name>.json` plus two logs under
`demo/recordings/<name>/`, then loaded with the server's replay reader. A pair
that fails to load is removed again, so a bad recording never breaks the list.
The race screen labels these pairs as replays of a live run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args

from data.load import load_all
from demo.stories import STORIES, RunView, Story, rescued_by, search_path
from so import config, eventlog
from so.contracts import Question, Setup

SETUPS: tuple[str, ...] = get_args(Setup)
RECORDINGS = "recordings"


@dataclass(frozen=True)
class PairSpec:
    """What to record: a question, the two setups and an optional story to verify."""

    name: str
    question: Question
    left: str = "single"
    right: str = "second_opinion"
    title: str | None = None
    story: Story | None = None


class RecordingError(RuntimeError):
    """A pair could not be recorded or did not load as a valid replay."""


def find_question(question_id: str) -> Question:
    for question in load_all():
        if question.id == question_id:
            return question
    raise RecordingError(f"Unknown question id: {question_id}")


def golden_specs(path: Path) -> list[PairSpec]:
    """Read the pairs chosen by `demo.candidates --write`."""
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["golden"]
    except FileNotFoundError:
        raise RecordingError(f"{path} does not exist; run python -m demo.candidates --write first") from None
    return [PairSpec(name=e["name"], question=find_question(e["question_id"]), left=e.get("left", "single"),
                     right=e.get("right", "second_opinion"), title=e.get("title"), story=e.get("story"))
            for e in entries]


def _validate_spec(spec: PairSpec) -> None:
    from server.app import SAFE_NAME

    if not SAFE_NAME.fullmatch(spec.name):
        raise RecordingError(f"Recording names must be URL-safe: {spec.name!r}")
    for setup in (spec.left, spec.right):
        if setup not in SETUPS:
            raise RecordingError(f"Unknown setup {setup!r}; choose from {', '.join(SETUPS)}")
    if spec.story is not None and spec.story not in STORIES:
        raise RecordingError(f"Unknown story {spec.story!r}; choose from {', '.join(STORIES)}")


async def _run_side(question: Question, setup: str, log_dir: Path) -> Path:
    from so.agent import run_question

    try:
        await run_question(question, setup, log_dir=log_dir)
    except Exception as exc:
        raise RecordingError(f"The {setup} run failed: {type(exc).__name__}: {exc}") from exc
    logs = list(log_dir.glob("*.jsonl"))
    if len(logs) != 1:
        raise RecordingError(f"Expected one {setup} log in {log_dir}, found {len(logs)}")
    return logs[0]


def story_holds(story: Story, left: RunView, right: RunView) -> bool:
    """Structural check of a fresh recording. Correctness still needs a human or the judge."""
    if story == "trap_decline":
        return left.status == "ANSWERED" and right.status == "NOT_FOUND"
    if story == "retry_recovery":
        return right.status == "ANSWERED" and rescued_by(right.events, "RETRY_ENGINE")
    return right.status == "ANSWERED" and rescued_by(right.events, "FETCH_PAGE")


async def record(spec: PairSpec, demo_dir: Path) -> dict[str, Any]:
    """Run both setups live, write the pair into `demo_dir` and verify that it loads."""
    from server.app import replay_catalog

    _validate_spec(spec)
    target = demo_dir / RECORDINGS / spec.name
    manifest = demo_dir / f"{spec.name}.json"
    if target.exists() or manifest.exists():
        raise RecordingError(f"A recording named {spec.name!r} already exists; remove it or pick a new name")
    previous_cache = config.SO_CACHE
    config.SO_CACHE = "off"
    try:
        with tempfile.TemporaryDirectory() as scratch:
            sides = [Path(scratch) / "left", Path(scratch) / "right"]
            left_log, right_log = await asyncio.gather(
                _run_side(spec.question, spec.left, sides[0]),
                _run_side(spec.question, spec.right, sides[1]))
            target.mkdir(parents=True)
            shutil.copyfile(left_log, target / "left.jsonl")
            shutil.copyfile(right_log, target / "right.jsonl")
    finally:
        config.SO_CACHE = previous_cache
    payload = {
        "title": spec.title or spec.question.question,
        "left": f"{RECORDINGS}/{spec.name}/left.jsonl",
        "right": f"{RECORDINGS}/{spec.name}/right.jsonl",
        "question_id": spec.question.id,
        "story": spec.story,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "live": True,
    }
    manifest.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        catalog = replay_catalog(demo_dir, None)
        if spec.name not in catalog:
            raise ValueError("the server did not list the new pair")
    except ValueError as exc:
        manifest.unlink(missing_ok=True)
        shutil.rmtree(target, ignore_errors=True)
        raise RecordingError(f"Recording {spec.name!r} did not load as a replay: {exc}") from exc
    left = RunView.from_events(spec.left, eventlog.read_log(target / "left.jsonl"))
    right = RunView.from_events(spec.right, eventlog.read_log(target / "right.jsonl"))
    return {"name": spec.name, "manifest": str(manifest), "left": left, "right": right,
            "story_holds": story_holds(spec.story, left, right) if spec.story else None}


def _report(spec: PairSpec, outcome: dict[str, Any]) -> None:
    print(f"\n{spec.name}: {spec.question.id}  {spec.question.question[:100]}")
    print(f"  gold: {', '.join(spec.question.gold)}")
    for view in (outcome["left"], outcome["right"]):
        totals = view.totals
        print(f"  {view.setup}: {view.status} | {view.final[:140]}")
        print(f"    {totals.get('engine_calls')} engine calls, ${totals.get('cost_usd_total', 0):.4f}, "
              f"{totals.get('latency_ms', 0) / 1000:.1f}s")
        for line in search_path(view.events):
            print(f"    search: {line}")
    if spec.story:
        verdict = "shows" if outcome["story_holds"] else "does NOT show"
        print(f"  this recording {verdict} the {spec.story} path; check the answers against gold before using it")
    print(f"  wrote {outcome['manifest']}")


async def _record_all(specs: list[PairSpec], demo_dir: Path) -> int:
    failures = 0
    for spec in specs:
        try:
            _report(spec, await record(spec, demo_dir))
        except RecordingError as exc:
            failures += 1
            print(f"\n{spec.name}: not recorded. {exc}")
    return failures


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--golden", action="store_true", help="record every pair listed in golden.json")
    parser.add_argument("--name")
    parser.add_argument("--question-id")
    parser.add_argument("--left", default="single", choices=SETUPS)
    parser.add_argument("--right", default="second_opinion", choices=SETUPS)
    parser.add_argument("--title")
    parser.add_argument("--story", choices=STORIES)
    parser.add_argument("--demo-dir", type=Path, default=Path(config.SERVER_DEMO_DIR))
    args = parser.parse_args(argv)
    if args.golden == bool(args.name or args.question_id):
        parser.error("use either --golden or both --name and --question-id")
    if args.golden:
        specs = golden_specs(args.demo_dir / "golden.json")
    elif not (args.name and args.question_id):
        parser.error("--name and --question-id are both required")
    else:
        specs = [PairSpec(args.name, find_question(args.question_id), args.left, args.right, args.title, args.story)]
    failures = asyncio.run(_record_all(specs, args.demo_dir))
    if failures:
        raise SystemExit(f"{failures} of {len(specs)} recording(s) failed")


if __name__ == "__main__":
    main()
