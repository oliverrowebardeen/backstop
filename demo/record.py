"""Record a live race between two setups as a replay pair for the race screen.

  python -m demo.record --golden                       # every entry in demo/golden.json
  python -m demo.record --name NAME --question-id ID [--left single] [--right second_opinion]
  python -m demo.record --golden --checker-backend cascade   # default: CHECKER_BACKEND
  python -m demo.record --name race-sqa-2963 --question-id sqa-2963 --left telem_default \
      --layout flat --demo-dir ui/fixtures --commit origin/main      # one shipped example
  python -m demo.record --sample N --seed S --layout flat --demo-dir DIR   # N drawn test questions

Both setups run at the same time on the same question with the response cache
off, so every search, page read and model call in the recording is live and its
timing is real. The checker backend is fixed for the whole recording and written
to the manifest. The decision and cascade backends need OPENAI_API_KEY, and a
recording is refused if a checked run made no Decisions call, because the
cascade falls back to the LLM checker without failing. Each pair is written as `demo/<name>.json` plus two logs under
`demo/recordings/<name>/`, then loaded with the server's replay reader. A pair
that fails to load is removed again, so a bad recording never breaks the list.
The race screen labels these pairs as replays of a live run.

`--sample N --seed S` draws N questions at random from one split's question file
(traps excluded) with `random.Random(S)`, after sorting by id, and Telem default
races Backstop on each. Which recordings ship is decided afterwards, so manifests
carry no selection claim. `--layout flat` writes the shipped-fixture layout
(`<name>.json` beside `<question id>.<setup>.jsonl`). Every manifest records the
commit whose agent code ran and the checker, routing and cache settings, which the
race screen shows as "Recorded live <date> on commit <sha> with ...".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args

from data.load import load_all, load_questions
from demo.stories import STORIES, RunView, Story, rescued_by, search_path
from so import config, eventlog
from so.contracts import Question, Setup

SETUPS: tuple[str, ...] = get_args(Setup)
CHECKER_BACKENDS = ("llm", "decision", "cascade")
RECORDINGS = "recordings"
# What a recording's behavior depends on. A commit label is only honest if these match it.
RUNTIME_PATHS = ("so", "data", "out/first_engine.json", "out/rescue_table.json")


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


def short_title(text: str, limit: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def sample_specs(n: int, seed: int, *, split: str = "test", left: str = "telem_default",
                 right: str = "second_opinion") -> list[PairSpec]:
    """n questions drawn at random from the split's question file (traps excluded), fixed by the seed."""
    pool = sorted(load_questions(split=split), key=lambda question: question.id)
    if not 1 <= n <= len(pool):
        raise RecordingError(f"Cannot draw {n} questions from {len(pool)} in the {split} split")
    picks = random.Random(seed).sample(pool, n)
    return [PairSpec(name=f"race-{question.id}", question=question, left=left, right=right,
                     title=short_title(question.question)) for question in picks]


def code_commit(rev: str) -> str:
    """The commit whose agent code is running. Refuses when so/, data/ or the routing tables differ from it."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", rev], cwd=config.ROOT, check=True,
                             capture_output=True, text=True).stdout.strip()
        changed = subprocess.run(["git", "diff", "--name-only", rev, "--", *RUNTIME_PATHS], cwd=config.ROOT,
                                 check=True, capture_output=True, text=True).stdout.split()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RecordingError(f"Cannot resolve commit {rev!r}: {exc}") from None
    if changed:
        raise RecordingError(f"The agent code differs from {rev} ({', '.join(changed[:5])}); commit it first")
    return sha


def _validate_spec(spec: PairSpec) -> None:
    from server.app import SAFE_NAME

    if not SAFE_NAME.fullmatch(spec.name):
        raise RecordingError(f"Recording names must be URL-safe: {spec.name!r}")
    for setup in (spec.left, spec.right):
        if setup not in SETUPS:
            raise RecordingError(f"Unknown setup {setup!r}; choose from {', '.join(SETUPS)}")
    if spec.story is not None and spec.story not in STORIES:
        raise RecordingError(f"Unknown story {spec.story!r}; choose from {', '.join(STORIES)}")


def _validate_backend(backend: str) -> None:
    if backend not in CHECKER_BACKENDS:
        raise RecordingError(f"Unknown checker backend {backend!r}; choose from {', '.join(CHECKER_BACKENDS)}")
    if backend != "llm":
        try:
            config.api_key("openai")
        except ValueError:
            raise RecordingError(f"The {backend} checker calls Decisions and needs OPENAI_API_KEY") from None


def _require_decisions(setup: str, events: list[dict[str, Any]], backend: str) -> None:
    """A checked run on the decision or cascade backend must show at least one Decisions call."""
    if backend == "llm" or not any(e["type"] == "check" for e in events):
        return
    if not any(e["type"] == "llm" and e["data"].get("role") == "checker"
               and e["data"].get("model") == config.DECISION_MODEL for e in events):
        raise RecordingError(f"The {setup} run made no {config.DECISION_MODEL} call, so it did not run the "
                             f"{backend} checker; check the Decisions key and endpoint")


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


async def record(spec: PairSpec, demo_dir: Path, *, checker_backend: str | None = None,
                 flat: bool = False, provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run both setups live, write the pair into `demo_dir` and verify that it loads.

    `checker_backend` defaults to CHECKER_BACKEND and holds for both runs. `flat` writes the
    shipped-fixture layout; `provenance` (commit, seed, selection) is copied into the manifest.
    """
    from server.app import replay_catalog

    backend = checker_backend or config.CHECKER_BACKEND
    _validate_spec(spec)
    _validate_backend(backend)
    if flat and spec.left == spec.right:
        raise RecordingError("The flat layout names logs by setup, so the two setups must differ")
    target = demo_dir if flat else demo_dir / RECORDINGS / spec.name
    names = ([f"{spec.question.id}.{spec.left}.jsonl", f"{spec.question.id}.{spec.right}.jsonl"] if flat
             else [f"{RECORDINGS}/{spec.name}/left.jsonl", f"{RECORDINGS}/{spec.name}/right.jsonl"])
    logs = [demo_dir / name for name in names]
    manifest = demo_dir / f"{spec.name}.json"
    if (not flat and target.exists()) or manifest.exists() or any(log.exists() for log in logs):
        raise RecordingError(f"A recording named {spec.name!r} already exists; remove it or pick a new name")
    previous = config.SO_CACHE, config.CHECKER_BACKEND
    config.SO_CACHE, config.CHECKER_BACKEND = "off", backend
    try:
        with tempfile.TemporaryDirectory() as scratch:
            sides = [Path(scratch) / "left", Path(scratch) / "right"]
            left_log, right_log = await asyncio.gather(
                _run_side(spec.question, spec.left, sides[0]),
                _run_side(spec.question, spec.right, sides[1]))
            for setup, log in ((spec.left, left_log), (spec.right, right_log)):
                _require_decisions(setup, eventlog.read_log(log), backend)
            target.mkdir(parents=True, exist_ok=flat)
            shutil.copyfile(left_log, logs[0])
            shutil.copyfile(right_log, logs[1])
    finally:
        config.SO_CACHE, config.CHECKER_BACKEND = previous
    payload = {
        "title": spec.title or spec.question.question,
        "left": names[0],
        "right": names[1],
        "question_id": spec.question.id,
        "story": spec.story,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "live": True,
        "checker_backend": backend,
        "first_engine_routing": config.FIRST_ENGINE_ROUTING,
        "cache": "off",
        **(provenance or {}),
    }
    if backend == "cascade":
        payload["checker_cascade_threshold"] = config.CHECKER_CASCADE_THRESHOLD
    manifest.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        catalog = replay_catalog(demo_dir, None)
        if spec.name not in catalog:
            raise ValueError("the server did not list the new pair")
    except ValueError as exc:
        manifest.unlink(missing_ok=True)
        for log in logs:
            log.unlink(missing_ok=True)
        if not flat:
            shutil.rmtree(target, ignore_errors=True)
        raise RecordingError(f"Recording {spec.name!r} did not load as a replay: {exc}") from exc
    left = RunView.from_events(spec.left, eventlog.read_log(logs[0]))
    right = RunView.from_events(spec.right, eventlog.read_log(logs[1]))
    return {"name": spec.name, "manifest": str(manifest), "left": left, "right": right, "checker_backend": backend,
            "story_holds": story_holds(spec.story, left, right) if spec.story else None}


def _report(spec: PairSpec, outcome: dict[str, Any]) -> None:
    print(f"\n{spec.name}: {spec.question.id}  {spec.question.question[:100]}")
    print(f"  gold: {', '.join(spec.question.gold)}")
    print(f"  checker backend: {outcome['checker_backend']}")
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


async def _record_all(specs: list[PairSpec], demo_dir: Path, checker_backend: str, *, flat: bool = False,
                      provenance: dict[str, Any] | None = None) -> int:
    failures = 0
    for spec in specs:
        try:
            _report(spec, await record(spec, demo_dir, checker_backend=checker_backend, flat=flat,
                                       provenance=provenance))
        except RecordingError as exc:
            failures += 1
            print(f"\n{spec.name}: not recorded. {exc}")
    return failures


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--golden", action="store_true", help="record every pair listed in golden.json")
    parser.add_argument("--sample", type=int, help="draw this many questions at random from --split")
    parser.add_argument("--seed", type=int, help="seed for --sample (required with it)")
    parser.add_argument("--split", default="test", choices=("learn", "test"))
    parser.add_argument("--layout", default="nested", choices=("nested", "flat"),
                        help="flat: <name>.json beside <question id>.<setup>.jsonl, as in ui/fixtures")
    parser.add_argument("--commit", default="HEAD", help="commit whose agent code is running (checked, then recorded)")
    parser.add_argument("--name")
    parser.add_argument("--question-id")
    parser.add_argument("--left", choices=SETUPS, help="default: telem_default with --sample, else single")
    parser.add_argument("--right", default="second_opinion", choices=SETUPS)
    parser.add_argument("--title")
    parser.add_argument("--story", choices=STORIES)
    parser.add_argument("--demo-dir", type=Path, default=Path(config.SERVER_DEMO_DIR))
    parser.add_argument("--checker-backend", choices=CHECKER_BACKENDS, default=config.CHECKER_BACKEND,
                        help="checker for every run in the recording (default: CHECKER_BACKEND); "
                             "decision and cascade need OPENAI_API_KEY")
    args = parser.parse_args(argv)
    modes = [args.golden, bool(args.name or args.question_id), args.sample is not None]
    if sum(modes) != 1:
        parser.error("use exactly one of --golden, --sample or --name with --question-id")
    provenance: dict[str, Any] = {}
    if args.sample is not None:
        if args.seed is None:
            parser.error("--sample needs --seed")
        specs = sample_specs(args.sample, args.seed, split=args.split, left=args.left or "telem_default",
                             right=args.right)
        provenance = {"split": args.split}
    elif args.golden:
        specs = golden_specs(args.demo_dir / "golden.json")
    elif not (args.name and args.question_id):
        parser.error("--name and --question-id are both required")
    else:
        specs = [PairSpec(args.name, find_question(args.question_id), args.left or "single", args.right, args.title,
                          args.story)]
    provenance["commit"] = code_commit(args.commit)
    failures = asyncio.run(_record_all(specs, args.demo_dir, args.checker_backend,
                                       flat=args.layout == "flat", provenance=provenance))
    if failures:
        raise SystemExit(f"{failures} of {len(specs)} recording(s) failed")


if __name__ == "__main__":
    main()
