"""Eval runner (lane S4): run the agent on every test question and trap in each setup.

  python -m evals.run_eval                          # 200 test + 30 traps, x 3 setups
  python -m evals.run_eval --limit 20               # dry run (see below)
  python -m evals.run_eval --setups single,second_opinion
  python -m evals.run_eval --traps-only
  python -m evals.run_eval --max-test 120           # the 2:30 cut: 120 test + all traps

Question order is stable: test questions are sorted by sha1(id), which spreads
fact and finance evenly, so any prefix is a fair sample.
  --limit N     first N test questions in that order plus a proportional share
                of traps (round(N * 30 / 200), at least 1). With --traps-only,
                the first N traps.
  --max-test N  first N test questions in that order plus ALL traps. Recorded
                in out/eval_manifest.json so evals.score reports the cut.

Resumable: a (question, setup) is done when some log in out/logs/ for it has a
run_end event. Logs without run_end (a crash, a kill) are ignored; the rerun
writes a fresh log with a new run_id.

Agent and event log come from so.agent / so.eventlog. With SO_FAKE=1 they come
from tests/s4/ instead and everything is written under out/fake/.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from data.load import load_questions, load_traps
from evals.deps import FAKE, OUT

SETUPS = ["single", "telem_default", "second_opinion"]
LOG_DIR = OUT / "logs"
MANIFEST = OUT / "eval_manifest.json"
FULL_TEST, FULL_TRAPS = 200, 30


# ---------------------------------------------------------------- dependencies

def load_agent():
    """so.agent normally; the tests/s4 fake only when SO_FAKE=1."""
    if FAKE:
        from tests.s4 import fake_agent
        return fake_agent
    from so import agent
    return agent


def load_eventlog():
    """so.eventlog normally; the tests/s4 fake only when SO_FAKE=1."""
    if FAKE:
        from tests.s4 import fake_eventlog
        return fake_eventlog
    from so import eventlog
    return eventlog


# ---------------------------------------------------------------- log scanning

def _scan_log(path: Path) -> tuple[str | None, str | None, dict | None]:
    """(question_id, setup, run_end data or None). Tolerates a torn last line."""
    qid = setup = end = None
    try:
        text = path.read_text()
    except OSError:
        return None, None, None
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        qid = qid or ev.get("question_id")
        setup = setup or ev.get("setup")
        if ev.get("type") == "run_end":
            end = ev.get("data") or {}
    return qid, setup, end


def finished_runs(log_dir: Path) -> dict[tuple[str, str], Path]:
    """{(question_id, setup): path} for every log that reached run_end.

    If a pair has several finished logs, the newest one wins.
    """
    out: dict[tuple[str, str], tuple[float, str, Path]] = {}
    if not Path(log_dir).exists():
        return {}
    for path in Path(log_dir).glob("*.jsonl"):
        qid, setup, end = _scan_log(path)
        if end is None or not qid or not setup:
            continue
        rank = (path.stat().st_mtime, path.name, path)
        if (qid, setup) not in out or rank[:2] > out[(qid, setup)][:2]:
            out[(qid, setup)] = rank
    return {k: v[2] for k, v in out.items()}


def run_end_cost(path: Path) -> float:
    _, _, end = _scan_log(path)
    return float((end or {}).get("cost_usd_total") or 0)


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


def write_manifest(test, traps, setups, args) -> dict:
    """Record what this eval covers. --traps-only keeps the earlier test set."""
    old = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    m = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "setups": sorted(set(setups) | set(old.get("setups", []))) if args.traps_only else setups,
        "test_ids": old.get("test_ids", []) if args.traps_only else [q.id for q in test],
        "trap_ids": [q.id for q in traps],
        "limit": old.get("limit") if args.traps_only else args.limit,
        "max_test": old.get("max_test") if args.traps_only else args.max_test,
    }
    n_test = len(m["test_ids"])
    m["n_test"], m["n_traps"] = n_test, len(m["trap_ids"])
    m["cut"] = bool(m["max_test"]) and n_test < FULL_TEST
    if m["cut"]:
        m["note"] = f"cut to {n_test} test questions (of {FULL_TEST}) plus {m['n_traps']} traps to finish in time"
    elif m["limit"]:
        m["note"] = f"dry run limited to {n_test} test questions plus {m['n_traps']} traps (--limit {m['limit']})"
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=2))
    return m


# ---------------------------------------------------------------- running

def _fmt_eta(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    return f"{seconds // 60}m{seconds % 60:02d}s"


async def _call_agent(agent, q, setup: str, log_dir: Path):
    fn = agent.run_question
    if inspect.iscoroutinefunction(fn):
        return await fn(q, setup, log_dir=str(log_dir))
    res = await asyncio.to_thread(fn, q, setup, log_dir=str(log_dir))
    return await res if inspect.isawaitable(res) else res


def _newest_finished(log_dir: Path, qid: str, setup: str, since: float) -> Path | None:
    """The log this run just finished, found by its run_id prefix."""
    best = None
    for path in Path(log_dir).glob(f"{setup}-{qid}-*.jsonl"):
        if path.stat().st_mtime + 1 < since:
            continue
        _, _, end = _scan_log(path)
        if end is not None and (best is None or path.stat().st_mtime > best.stat().st_mtime):
            best = path
    return best


async def run(test, traps, setups: list[str], log_dir: Path = LOG_DIR, concurrency: int | None = None) -> dict:
    agent = load_agent()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    done = finished_runs(log_dir)
    prior_spend = sum(run_end_cost(p) for p in done.values())
    pairs = [(q, s) for s in setups for q in list(test) + list(traps)]
    todo = [(q, s) for q, s in pairs if (q.id, s) not in done]
    print(f"{len(pairs)} runs ({len(test)} test + {len(traps)} traps x {len(setups)} setups: "
          f"{', '.join(setups)}). {len(pairs) - len(todo)} already finished, {len(todo)} to run. "
          f"prior spend ${prior_spend:.2f}. logs: {log_dir}", flush=True)
    concurrency = concurrency or int(os.getenv("LLM_MAX_CONCURRENCY", 8))
    sem = asyncio.Semaphore(concurrency)
    state = {"done": 0, "spend": 0.0, "failures": [], "stop": None}
    t0 = time.time()

    def progress() -> str:
        n, total = state["done"], len(todo)
        el = time.time() - t0
        eta = el / n * (total - n) if n else 0
        return (f"  {n}/{total} runs, spend ${state['spend']:.3f} this run "
                f"(${prior_spend + state['spend']:.2f} total), {_fmt_eta(el)} elapsed, ETA {_fmt_eta(eta)}, "
                f"{len(state['failures'])} failed")

    async def one(q, setup):
        async with sem:
            if state["stop"]:
                return
            started = time.time()
            try:
                await _call_agent(agent, q, setup, log_dir)
                path = _newest_finished(log_dir, q.id, setup, started)
                if path is not None:
                    state["spend"] += run_end_cost(path)
                else:
                    state["failures"].append((q.id, setup, "agent returned but no log with run_end was found"))
            except Exception as e:  # noqa: BLE001  one bad run must not stop the eval
                msg = f"{type(e).__name__}: {e}"
                state["failures"].append((q.id, setup, msg))
                print(f"  ! {setup} {q.id}: {msg}", file=sys.stderr, flush=True)
                tb = traceback.format_exc(limit=3).strip().splitlines()
                print("    " + "\n    ".join(tb[-3:]), file=sys.stderr, flush=True)
                if "Budget" in type(e).__name__:
                    state["stop"] = msg
            state["done"] += 1
            if state["done"] % 5 == 0 or state["done"] == len(todo):
                print(progress(), flush=True)

    await asyncio.gather(*(one(q, s) for q, s in todo))
    print("done. " + progress().strip())
    if state["stop"]:
        print(f"STOPPED on budget: {state['stop']}. Raise TELEM_BUDGET_USD or cut the set, then re-run.")
    if state["failures"]:
        print(f"{len(state['failures'])} runs failed (re-run to retry them; finished runs are skipped):")
        for qid, setup, msg in state["failures"]:
            print(f"  {setup:15} {qid:12} {msg}")
    return state


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, help="first N test questions plus proportional traps")
    p.add_argument("--setups", default=",".join(SETUPS), help="comma separated, default all three")
    p.add_argument("--traps-only", action="store_true")
    p.add_argument("--max-test", type=int, help="cut to the first N test questions plus all traps")
    p.add_argument("--concurrency", type=int, help="default LLM_MAX_CONCURRENCY or 8")
    args = p.parse_args(argv)
    setups = [s.strip() for s in args.setups.split(",") if s.strip()]
    bad = [s for s in setups if s not in SETUPS]
    if bad:
        p.error(f"unknown setup(s): {', '.join(bad)}. choose from {', '.join(SETUPS)}")
    test, traps = select(args.limit, args.max_test, args.traps_only)
    m = write_manifest(test, traps, setups, args)
    if m.get("note"):
        print(f"note: {m['note']}")
    asyncio.run(run(test, traps, setups, LOG_DIR, args.concurrency))


if __name__ == "__main__":
    main()
