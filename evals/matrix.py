"""Hit matrix: which engine finds the gold answer for which question.

For all 330 questions (300 + 30 traps): search_engines(question, ENGINES) with the
question text as query and goal, judge each engine's top 5, write
out/matrix.jsonl. Then search_default once per question, write
out/default_matrix.jsonl. Raw results go to out/matrix_results.jsonl so the
hand check and the checker eval can show exactly what the judge saw.

Async, resumable (skips rows already written), cached (Telem wrapper + judge cache).

  python -m evals.matrix --limit 10     # smoke test
  python -m evals.matrix                # full run
  python -m evals.matrix --summary      # hit rates per engine, learn and test
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections import defaultdict

from data.load import load_all
from evals import contamination, deps, judge
from evals.deps import ENGINES, get


def matrix_path():
    return deps.OUT / "matrix.jsonl"


def default_matrix_path():
    return deps.OUT / "default_matrix.jsonl"


def results_path():
    return deps.OUT / "matrix_results.jsonl"


TOP_K = 5  # Top 5 results per engine (--top-k overrides)

SPEND = {"search_usd": 0.0, "search_calls": 0, "cached_calls": 0, "errors": 0}
_lock = asyncio.Lock()


def _read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # a half-written last line from an interrupted run
    return rows


async def _append(path, row: dict) -> None:
    async with _lock:
        with path.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _results_dump(results) -> list[dict]:
    return [{"rank": get(r, "rank"), "title": get(r, "title"), "url": get(r, "url"),
             "snippet": get(r, "snippet"), "published": get(r, "published"),
             "engine": get(r, "engine")} for r in results]


def _count_spend(run) -> None:
    SPEND["search_calls"] += 1
    if get(run, "cached"):
        SPEND["cached_calls"] += 1
    else:
        SPEND["search_usd"] += float(get(run, "cost_usd", 0) or 0)


def _spend_line(done: int, total: int, t0: float) -> str:
    s = SPEND["search_usd"] + judge.SPEND["judge_usd"]
    return (f"[{done}/{total}] spend ${s:.3f} (search ${SPEND['search_usd']:.3f}, "
            f"judge ${judge.SPEND['judge_usd']:.3f}) | calls {SPEND['search_calls']} "
            f"({SPEND['cached_calls']} cached), judge {judge.SPEND['judge_calls']} "
            f"(+{judge.SPEND['judge_cached']} cached) | errors {SPEND['errors']} | {time.time() - t0:.0f}s")


async def _judge_row(q, results: list) -> dict:
    """Drop benchmark copies, judge the rest, report hit_rank as the position in `results`."""
    kept, n_contam = contamination.filter_results(q.question, results)
    hit, rank, reason = await judge.judge_hit(q, kept)
    if hit and rank is not None:
        rank = next(i for i, r in enumerate(results, 1) if r is kept[rank - 1])
    return {"hit": hit, "hit_rank": rank, "judge_reason": reason, "n_contaminated": n_contam}


async def _engines_for_question(q, engines: list[str]) -> None:
    runs = await deps.telem_client.search_engines(q.question, engines, num_results=TOP_K, goal=q.question)
    for engine in engines:
        run = runs.get(engine)
        if run is None or get(run, "status") != "ok":
            SPEND["errors"] += 1
            print(f"  ! {q.id} {engine}: {get(run, 'status', 'missing')} (not written, will retry on re-run)",
                  file=sys.stderr)
            continue
        _count_spend(run)
        results = list(get(run, "results") or [])[:TOP_K]
        verdict = await _judge_row(q, results)
        await _append(results_path(), {"question_id": q.id, "engine": engine, "results": _results_dump(results)})
        await _append(matrix_path(), {
            "question_id": q.id, "engine": engine, **verdict, "n_results": len(results),
            "latency_ms": get(run, "latency_ms", 0), "cost_usd": get(run, "cost_usd", 0),
        })


async def _default_for_question(q) -> None:
    runs = await deps.telem_client.search_default(q.question, goal=q.question)
    ok = {e: r for e, r in runs.items() if get(r, "status") == "ok"}
    if not ok:
        SPEND["errors"] += 1
        print(f"  ! {q.id} telem_default: no engine returned ok (will retry)", file=sys.stderr)
        return
    combined = []
    for e, r in runs.items():
        _count_spend(r)
    for e, r in ok.items():
        combined.extend(list(get(r, "results") or [])[:TOP_K])
    verdict = await _judge_row(q, combined)
    await _append(results_path(), {"question_id": q.id, "engine": "telem_default", "results": _results_dump(combined)})
    await _append(default_matrix_path(), {
        "question_id": q.id, "engine": "telem_default", **verdict, "n_results": len(combined),
        # Telem queries its engines in parallel: wall clock is the slowest one.
        "latency_ms": max(float(get(r, "latency_ms", 0) or 0) for r in runs.values()),
        "cost_usd": sum(float(get(r, "cost_usd", 0) or 0) for r in runs.values()),
        "engines_used": sorted(runs.keys()),
    })


async def run(limit: int | None, concurrency: int, skip_default: bool, record_errors: bool = False) -> None:
    deps.OUT.mkdir(parents=True, exist_ok=True)
    qs = load_all()
    if limit:
        # Spread the smoke test over both categories and traps.
        qs = sorted(qs, key=lambda q: hashlib.sha1(q.id.encode()).hexdigest())[:limit]
    done_pairs = {(r["question_id"], r["engine"]) for r in _read_jsonl(matrix_path())}
    done_default = {r["question_id"] for r in _read_jsonl(default_matrix_path())}
    todo = []
    for q in qs:
        missing = [e for e in ENGINES if (q.id, e) not in done_pairs]
        need_default = not skip_default and q.id not in done_default
        if missing or need_default:
            todo.append((q, missing, need_default))
    print(f"{len(qs)} questions, {len(todo)} with work left. engines: {', '.join(ENGINES)}")
    sem = asyncio.Semaphore(concurrency)
    t0 = time.time()
    done = 0

    async def one(q, missing, need_default):
        nonlocal done
        async with sem:
            try:
                if missing:
                    await _engines_for_question(q, missing)
                if need_default:
                    await _default_for_question(q)
            except Exception as e:  # noqa: BLE001
                name = type(e).__name__
                if "Budget" in name:
                    raise
                SPEND["errors"] += 1
                print(f"  ! {q.id}: {name}: {e}", file=sys.stderr)
                if record_errors:
                    # A persistent engine failure is a miss with zero results; say why.
                    for engine in missing:
                        await _append(matrix_path(), {
                            "question_id": q.id, "engine": engine, "hit": False, "hit_rank": None,
                            "judge_reason": f"engine error after retries: {name}: {str(e)[:160]}",
                            "n_contaminated": 0, "n_results": 0, "latency_ms": 0, "cost_usd": 0,
                            "engine_error": True})
            done += 1
            if done % 5 == 0 or done == len(todo):
                print(_spend_line(done, len(todo), t0), flush=True)

    await asyncio.gather(*(one(*t) for t in todo))
    print("done. " + _spend_line(done, len(todo), t0))
    if SPEND["errors"] and record_errors:
        print(f"{SPEND['errors']} engine errors recorded as zero-result misses (engine_error: true).")
    elif SPEND["errors"]:
        print(f"{SPEND['errors']} engine errors were not written. Re-run to retry them, "
              "or pass --record-errors if the engine fails persistently.")


async def rejudge(concurrency: int) -> None:
    """Recompute hit fields for every row from the stored raw results (no new searches).

    Used after a judge or contamination-filter change. Unchanged judge inputs come
    from the judge cache at no cost. Files are rewritten atomically.
    """
    qs = {q.id: q for q in load_all()}
    stored = {(r["question_id"], r["engine"]): r["results"] for r in _read_jsonl(results_path())}
    sem = asyncio.Semaphore(concurrency)

    async def redo(row):
        async with sem:
            if row.get("engine_error"):
                return row  # no results were ever returned; nothing to rejudge
            results = stored.get((row["question_id"], row["engine"]))
            if results is None:
                raise RuntimeError(f"no stored results for {row['question_id']} {row['engine']}")
            return {**row, **await _judge_row(qs[row["question_id"]], results)}

    for path in (matrix_path(), default_matrix_path()):
        rows = _read_jsonl(path)
        new = await asyncio.gather(*(redo(r) for r in rows))
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in new))
        tmp.replace(path)
        changed = sum(a["hit"] != b["hit"] for a, b in zip(rows, new))
        dropped = sum(r["n_contaminated"] for r in new)
        print(f"{path.name}: {len(new)} rows rejudged, {changed} hit labels changed, "
              f"{dropped} benchmark-copy results excluded")
    print(f"judge spend ${judge.SPEND['judge_usd']:.3f} ({judge.SPEND['judge_calls']} calls, "
          f"{judge.SPEND['judge_cached']} cached)")


def summary() -> dict:
    split_of = {q.id: q.split for q in load_all()}
    cat_of = {q.id: q.category for q in load_all()}
    rows = _read_jsonl(matrix_path()) + _read_jsonl(default_matrix_path())
    agg = defaultdict(lambda: [0, 0])  # (engine, bucket) -> [hits, n]
    for r in rows:
        qid = r["question_id"]
        bucket = "traps" if cat_of.get(qid) == "trap" else split_of.get(qid, "?")
        agg[(r["engine"], bucket)][0] += int(bool(r["hit"]))
        agg[(r["engine"], bucket)][1] += 1
    engines = ENGINES + ["telem_default"]
    buckets = ["learn", "test", "traps"]
    print(f"{'engine':15}" + "".join(f"{b:>18}" for b in buckets))
    out = {}
    for e in engines:
        cells = []
        for b in buckets:
            h, n = agg.get((e, b), [0, 0])
            out.setdefault(e, {})[b] = {"hits": h, "n": n, "rate": round(h / n, 3) if n else None}
            cells.append(f"{(h / n if n else 0):>7.1%} ({h:>3}/{n:<3})" if n else f"{'-':>18}")
        print(f"{e:15}" + "".join(f"{c:>18}" for c in cells))
    expected = len(load_all())
    have = {r["question_id"] for r in _read_jsonl(matrix_path())}
    print(f"\nquestions with matrix rows: {len(have)}/{expected}. "
          "Traps: hit = results contain evidence the premise is false.")
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int)
    p.add_argument("--summary", action="store_true")
    p.add_argument("--rejudge", action="store_true",
                   help="recompute hits from stored results (after a judge/filter change)")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--no-default", action="store_true", help="skip the Telem default pass")
    p.add_argument("--top-k", type=int, default=TOP_K, help="results judged per engine")
    p.add_argument("--record-errors", action="store_true",
                   help="write persistent engine failures as zero-result misses (flagged engine_error)")
    a = p.parse_args()
    globals()["TOP_K"] = a.top_k
    if a.summary:
        summary()
        return
    if a.rejudge:
        asyncio.run(rejudge(a.concurrency))
        return
    asyncio.run(run(a.limit, a.concurrency, a.no_default, a.record_errors))


if __name__ == "__main__":
    main()
