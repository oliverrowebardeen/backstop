"""Eval runner (lane S4): run the agent on every test question and trap in each setup.

  python -m evals.run_eval                          # all test questions + traps, x every setup
  python -m evals.run_eval --limit 20               # dry run (see below)
  python -m evals.run_eval --setups single,second_opinion
  python -m evals.run_eval --traps-only
  python -m evals.run_eval --max-test 120           # the 2:30 cut: 120 test + all traps

Question order is stable: test questions and traps are each sorted by sha1(id),
which spreads fact and finance evenly, so any prefix is a fair sample.
  --limit N     first N test questions in that order plus the same share of
                traps (N / test questions x traps, rounded, at least 1). With
                --traps-only, the first N traps.
  --max-test N  first N test questions in that order plus ALL traps. Recorded
                in out/eval_manifest.json so evals.score reports the cut.

Resumable: a (question, setup) is done when some log in out/logs/ for it has a
run_end event, no error and an answer. Logs without one (a crash, a kill, an
agent error) are ignored; the rerun
writes a fresh log with a new run_id.

Logs are read with so.eventlog. The agent is so.agent unless AGENT is set to
another module with the same run_question; outputs go under evals.deps.OUT.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

from data.load import load_questions, load_traps
from evals import contamination, deps
from so import eventlog
from so.config import LLM_MAX_CONCURRENCY
from so.contracts import Setup
from so.telem_client import RESULT_FILTER

SETUPS: list[str] = list(get_args(Setup))
MANIFEST_NAME = "eval_manifest.json"
# Per run: how many search results the benchmark-contamination filter removed.
CONTAMINATION_NAME = "eval_contamination.jsonl"
# The module whose run_question the eval drives. None means so.agent (imported on first use).
AGENT = None


def default_log_dir() -> Path:
    return deps.OUT / "logs"


def manifest_path(log_dir: Path | None = None) -> Path:
    """The default log dir shares out/eval_manifest.json; any other log dir gets a sibling of its own."""
    if log_dir is None or Path(log_dir) == default_log_dir():
        return deps.OUT / MANIFEST_NAME
    return Path(log_dir).parent / f"{Path(log_dir).name}.manifest.json"


def contamination_path(log_dir: Path) -> Path:
    """Per-run contamination counts, kept next to (never inside) the log directory."""
    if Path(log_dir) == default_log_dir():
        return Path(log_dir).parent / CONTAMINATION_NAME
    return Path(log_dir).parent / f"{Path(log_dir).name}.contamination.jsonl"


def load_agent():
    if AGENT is not None:
        return AGENT
    from so import agent
    return agent


# ---------------------------------------------------------------- log scanning

def run_end(path: Path) -> dict | None:
    """The run_end event of a log, or None if the run never finished or the log is unreadable."""
    try:
        events = eventlog.read_log(path)
    except (OSError, ValueError) as e:  # ValueError covers bad JSON and pydantic validation
        print(f"  ! ignoring unreadable log {Path(path).name}: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    return next((e for e in reversed(events) if e["type"] == "run_end"), None)


def completed(path: Path) -> dict | None:
    """The run_end event of a run that produced an answer, else None.

    The agent writes run_end even when it raises (data.error holds the exception
    type), so a log with run_end is not necessarily a finished run. Only a run with
    an answer event and no error counts; anything else is retried on the next pass.
    """
    end = run_end(path)
    if end is None or end["data"].get("error"):
        return None
    events = eventlog.read_log(path)
    return end if any(e["type"] == "answer" for e in events) else None


def finished_runs(log_dir: Path) -> dict[tuple[str, str], Path]:
    """{(question_id, setup): path} for every completed run (see `completed`). The newest log wins."""
    best: dict[tuple[str, str], tuple[float, str, Path]] = {}
    for path in Path(log_dir).glob("*.jsonl"):
        end = completed(path)
        if end is None:
            continue
        key, rank = (end["question_id"], end["setup"]), (path.stat().st_mtime, path.name, path)
        if key not in best or rank[:2] > best[key][:2]:
            best[key] = rank
    return {k: v[2] for k, v in best.items()}


def run_cost(path: Path) -> float:
    return float(eventlog.summarize(path)["cost_usd_total"])


def _finished_since(log_dir: Path, qid: str, setup: str, since: float) -> Path | None:
    """The finished log this process just wrote for (qid, setup), found by its run_id prefix."""
    fresh = [p for p in Path(log_dir).glob(f"{setup}-{qid}-*.jsonl") if p.stat().st_mtime >= since]
    return next((p for p in sorted(fresh, key=lambda p: p.stat().st_mtime, reverse=True) if completed(p)), None)


# ---------------------------------------------------------------- selection

def _stable(qs):
    return sorted(qs, key=lambda q: hashlib.sha1(q.id.encode()).hexdigest())


def select(limit: int | None = None, max_test: int | None = None, traps_only: bool = False):
    """(test questions, traps) to run, in a stable order."""
    test = _stable(load_questions(split="test"))
    traps = _stable([t for t in load_traps() if t.split == "test"])
    if traps_only:
        return [], traps[:limit] if limit else traps
    if max_test:
        test = test[:max_test]
    if limit:
        n_traps = max(1, round(limit * len(traps) / max(len(test), 1)))
        test, traps = test[:limit], traps[:n_traps]
    return test, traps


def _record_contamination(path: Path, row: dict) -> None:
    """Append one run's filter counts; a failed write warns and never fails the run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        print(f"  ! could not record contamination counts in {path}: {e}", file=sys.stderr)


def write_manifest(test, traps, setups, limit, max_test, traps_only, path: Path | None = None) -> dict:
    """Record what this eval covers. --traps-only keeps the earlier test set."""
    path = manifest_path() if path is None else Path(path)
    old = json.loads(path.read_text()) if path.exists() else {}
    keep = traps_only and bool(old)
    m = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "setups": sorted(set(setups) | set(old.get("setups", []))) if keep else setups,
        "test_ids": old.get("test_ids", []) if keep else [q.id for q in test],
        "trap_ids": [q.id for q in traps],
        "limit": old.get("limit") if keep else limit,
        "max_test": old.get("max_test") if keep else max_test,
    }
    full_test = len(load_questions(split="test"))
    m["n_test"], m["n_traps"] = len(m["test_ids"]), len(m["trap_ids"])
    m["cut"] = bool(m["max_test"]) and m["n_test"] < full_test
    if m["cut"]:
        m["note"] = (f"cut to {m['n_test']} test questions (of {full_test}) plus {m['n_traps']} traps "
                     "to finish in time")
    elif m["limit"]:
        m["note"] = (f"dry run limited to {m['n_test']} test questions plus {m['n_traps']} traps "
                     f"(--limit {m['limit']})")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(m, indent=2))
    return m


# ---------------------------------------------------------------- running

def _fmt_eta(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    return f"{seconds // 60}m{seconds % 60:02d}s"


async def _call_agent(agent, q, setup: str, log_dir: Path):
    """Await run_question whether it is async or blocking."""
    fn = agent.run_question
    if inspect.iscoroutinefunction(fn):
        return await fn(q, setup, log_dir=str(log_dir))
    return await asyncio.to_thread(fn, q, setup, log_dir=str(log_dir))


async def run(test, traps, setups: list[str], log_dir: Path | None = None,
              concurrency: int = LLM_MAX_CONCURRENCY) -> dict:
    agent = load_agent()
    log_dir = default_log_dir() if log_dir is None else Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    done = finished_runs(log_dir)
    prior_spend = sum(run_cost(p) for p in done.values())
    pairs = [(q, s) for s in setups for q in list(test) + list(traps)]
    todo = [(q, s) for q, s in pairs if (q.id, s) not in done]
    print(f"{len(pairs)} runs ({len(test)} test + {len(traps)} traps x {len(setups)} setups: "
          f"{', '.join(setups)}). {len(pairs) - len(todo)} already finished, {len(todo)} to run. "
          f"prior spend ${prior_spend:.2f}. logs: {log_dir}", flush=True)
    sem = asyncio.Semaphore(concurrency)
    state = {"done": 0, "spend": 0.0, "failures": [], "stop": None}
    t0 = time.time()

    def progress() -> str:
        n, total = state["done"], len(todo)
        elapsed = time.time() - t0
        eta = elapsed / n * (total - n) if n else 0
        return (f"{n}/{total} runs, spend ${state['spend']:.3f} this run "
                f"(${prior_spend + state['spend']:.2f} total), {_fmt_eta(elapsed)} elapsed, "
                f"ETA {_fmt_eta(eta)}, {len(state['failures'])} failed")

    async def one(q, setup):
        async with sem:
            if state["stop"]:
                return
            started = time.time()
            seen = {"results": 0, "removed": 0}

            def drop_benchmark_copies(results, question=q.question):
                kept, removed = contamination.filter_results(question, results)
                seen["results"] += len(results)
                seen["removed"] += removed
                return kept

            # Same filter for every setup, the checker and the agent (see so.telem_client.RESULT_FILTER).
            token = RESULT_FILTER.set(drop_benchmark_copies)
            try:
                try:
                    await _call_agent(agent, q, setup, log_dir)
                finally:
                    RESULT_FILTER.reset(token)
                _record_contamination(contamination_path(log_dir),
                                      {"question_id": q.id, "setup": setup, **seen})
                path = _finished_since(log_dir, q.id, setup, started - 1)
                if path is None:
                    state["failures"].append((q.id, setup, "agent returned but no log with run_end was found"))
                else:
                    state["spend"] += run_cost(path)
            except Exception as e:  # noqa: BLE001  one bad run must not stop the eval
                msg = f"{type(e).__name__}: {e}"
                state["failures"].append((q.id, setup, msg))
                tb = traceback.format_exc(limit=3).strip().splitlines()[-3:]
                print(f"  ! {setup} {q.id}: {msg}\n    " + "\n    ".join(tb), file=sys.stderr, flush=True)
                if "Budget" in type(e).__name__:
                    state["stop"] = msg
            state["done"] += 1
            if state["done"] % 5 == 0 or state["done"] == len(todo):
                print("  " + progress(), flush=True)

    await asyncio.gather(*(one(q, s) for q, s in todo))
    print("done. " + progress())
    if state["stop"]:
        print(f"STOPPED on budget: {state['stop']}. Raise the budget or cut the set, then re-run.")
    if state["failures"]:
        print(f"{len(state['failures'])} runs failed (re-run to retry them; finished runs are skipped):")
        for qid, setup, msg in state["failures"]:
            print(f"  {setup:15} {qid:12} {msg}")
    return state


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, help="first N test questions plus a proportional share of traps")
    p.add_argument("--setups", default=",".join(SETUPS), help="comma separated, default all")
    p.add_argument("--traps-only", action="store_true")
    p.add_argument("--max-test", type=int, help="cut to the first N test questions plus all traps")
    p.add_argument("--concurrency", type=int, default=LLM_MAX_CONCURRENCY,
                   help="default so.config.LLM_MAX_CONCURRENCY")
    p.add_argument("--log-dir", type=Path, default=None,
                   help="write run logs here instead of out/logs (for an extra pass, e.g. another checker "
                        "backend); its manifest and contamination counts are written next to it")
    args = p.parse_args(argv)
    log_dir = args.log_dir or default_log_dir()
    setups = [s.strip() for s in args.setups.split(",") if s.strip()]
    bad = [s for s in setups if s not in SETUPS]
    if bad:
        p.error(f"unknown setup(s): {', '.join(bad)}. choose from {', '.join(SETUPS)}")
    test, traps = select(args.limit, args.max_test, args.traps_only)
    m = write_manifest(test, traps, setups, args.limit, args.max_test, args.traps_only,
                       path=manifest_path(log_dir))
    if m.get("note"):
        print(f"note: {m['note']}")
    asyncio.run(run(test, traps, setups, log_dir, args.concurrency))


if __name__ == "__main__":
    main()
