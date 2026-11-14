"""Score the eval (lane S4): out/results.json (section 6.12) and out/results.md.

  python -m evals.score

Reads every finished log in out/logs/ (a log with run_end), grades the final
answer with evals.judge.grade_answer (cached on disk), and totals costs from the
log. With SO_FAKE=1 everything is read from and written to out/fake/.

Definitions (sections 6.12 and 7):
- Setup blocks cover test, non-trap questions. Traps are reported separately.
- accuracy = correct / n, wrong_rate = wrong / n (n = questions scored).
- Costs come from run_end, minus any llm event with role "judge". Judge spend is
  reported on its own and never counted in a setup.
- cost_per_correct = total cost / correct answers.
- cost_per_search_call = (search cost + checker llm cost) / search tool calls.
  Search cost is every `search` event plus every `fetch` inside a search tool
  call (between a search and its search_done, i.e. Second Opinion's page-read
  rescue). The agent's own read_page fetches are not search calls. Search tool
  calls = number of search_done events.
- Checker: in second_opinion logs of test non-trap questions, the first check
  after a search on FIRST_ENGINE alone, joined with the matrix row
  (question_id, FIRST_ENGINE). A matrix hit is the truth label.
- If the set was scoped in out/eval_manifest.json (--limit, --max-test), only
  those questions are scored and the note is carried into both outputs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from data.load import load_questions, load_traps
from evals.deps import OUT, get
from evals.run_eval import LOG_DIR, MANIFEST, SETUPS, finished_runs, load_eventlog

FIRST_ENGINE = "tavily"
GRADES = ("correct", "wrong", "declined")
BASELINES = ("single", "telem_default")
RESULTS_JSON = OUT / "results.json"
RESULTS_MD = OUT / "results.md"
MATRIX = OUT / "matrix.jsonl"
AGREEMENT = OUT / "judge_agreement.json"

try:
    from so.config import FIRST_ENGINE  # noqa: F811
except ImportError:
    pass


# ---------------------------------------------------------------- per log

def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _ev(e) -> tuple[str, dict]:
    data = get(e, "data") or {}
    if not isinstance(data, dict):
        data = data.model_dump() if hasattr(data, "model_dump") else dict(data)
    return get(e, "type"), data


def search_call_cost(events) -> tuple[float, int]:
    """(search + in-call fetch + checker llm cost, number of search tool calls)."""
    cost, calls, in_call = 0.0, 0, False
    for e in events:
        t, d = _ev(e)
        if t == "search":
            in_call = True
            cost += _f(d.get("cost_usd"))
        elif t == "fetch" and in_call:
            cost += _f(d.get("cost_usd"))
        elif t == "llm" and d.get("role") == "checker":
            cost += _f(d.get("cost_usd"))
        elif t == "search_done":
            in_call = False
            calls += 1
    return cost, calls


def first_engine_checks(events, first_engine: str = FIRST_ENGINE) -> list[str]:
    """Verdicts of the first check after each search whose engines == [first_engine]."""
    out, pending = [], False
    for e in events:
        t, d = _ev(e)
        if t == "search":
            pending = list(d.get("engines") or []) == [first_engine]
        elif t == "check" and pending:
            out.append(str(d.get("verdict")))
            pending = False
    return out


def log_metrics(events, summary: dict | None) -> dict:
    """Costs, calls and latency of one finished run, judge cost excluded."""
    s = dict(summary or {})
    judge = sum(_f(d.get("cost_usd")) for t, d in map(_ev, events) if t == "llm" and d.get("role") == "judge")
    if not s:  # no run_end totals: rebuild them from events
        for t, d in map(_ev, events):
            if t == "search":
                s["engine_calls"] = s.get("engine_calls", 0) + len(d.get("engines") or [])
                s["cost_usd_search"] = _f(s.get("cost_usd_search")) + _f(d.get("cost_usd"))
            elif t == "fetch":
                s["cost_usd_search"] = _f(s.get("cost_usd_search")) + _f(d.get("cost_usd"))
            elif t == "llm":
                s["cost_usd_llm"] = _f(s.get("cost_usd_llm")) + _f(d.get("cost_usd"))
        s["cost_usd_total"] = _f(s.get("cost_usd_search")) + _f(s.get("cost_usd_llm"))
    sc_cost, sc_calls = search_call_cost(events)
    answer = next((d for t, d in map(_ev, events) if t == "answer"), None)
    return {
        "engine_calls": _f(s.get("engine_calls")),
        "cost_search": _f(s.get("cost_usd_search")),
        "cost_total": _f(s.get("cost_usd_total")) - judge,
        "judge_cost": judge,
        "latency_ms": _f(s.get("latency_ms")),
        "search_call_cost": sc_cost,
        "search_calls": sc_calls or int(_f(s.get("searches"))),
        "answer": answer,
    }


# ---------------------------------------------------------------- aggregates

def _rate(k, n):
    return round(k / n, 4) if n else 0.0


def setup_block(test_recs: list[dict], trap_recs: list[dict]) -> dict:
    """One section 6.12 setup block. Each record: log_metrics(...) plus 'grade'."""
    n = len(test_recs)
    g = Counter(r["grade"] for r in test_recs)
    cost_total = sum(r["cost_total"] for r in test_recs)
    sc_cost = sum(r["search_call_cost"] for r in test_recs)
    sc_calls = sum(r["search_calls"] for r in test_recs)
    lat = [r["latency_ms"] for r in test_recs]
    tg = Counter(r["grade"] for r in trap_recs)
    return {
        "n": n,
        "correct": g["correct"], "wrong": g["wrong"], "declined": g["declined"],
        "accuracy": _rate(g["correct"], n), "wrong_rate": _rate(g["wrong"], n),
        "engine_calls": int(sum(r["engine_calls"] for r in test_recs)),
        "engine_calls_per_q": round(sum(r["engine_calls"] for r in test_recs) / n, 3) if n else 0.0,
        "cost_search": round(sum(r["cost_search"] for r in test_recs), 6),
        "cost_total": round(cost_total, 6),
        "cost_per_q_search": round(sum(r["cost_search"] for r in test_recs) / n, 6) if n else 0.0,
        "cost_per_q_total": round(cost_total / n, 6) if n else 0.0,
        "cost_per_correct": round(cost_total / g["correct"], 6) if g["correct"] else None,
        "search_calls": sc_calls,
        "search_call_cost": round(sc_cost, 6),
        "cost_per_search_call": round(sc_cost / sc_calls, 6) if sc_calls else None,
        "latency_p50_ms": round(float(np.percentile(lat, 50))) if lat else 0,
        "latency_p95_ms": round(float(np.percentile(lat, 95))) if lat else 0,
        "trap_n": len(trap_recs),
        "trap_confident_wrong": tg["wrong"],
        "trap_correct": tg["correct"],
        "trap_declined": tg["declined"],
    }


def checker_metrics(joined: list[tuple[str, str]], matrix: dict[tuple[str, str], bool],
                    engines: list[str], first_engine: str = FIRST_ENGINE) -> dict:
    """joined: (question_id, verdict) for first-engine checks."""
    c = Counter()
    for qid, verdict in joined:
        truth = matrix.get((qid, first_engine))
        if truth is None:
            c["unjoined"] += 1
            continue
        c["n"] += 1
        flagged = verdict != "HIT"
        if truth:
            c["true_hits"] += 1
            c["hits_flagged"] += flagged
        else:
            c["true_misses"] += 1
            c["misses_flagged"] += flagged
            others = [matrix[(qid, e)] for e in engines if e != first_engine and (qid, e) in matrix]
            if others:
                c["dead_scored"] += 1
                c["dead_correct"] += (verdict == "DEAD") == (not any(others))
    return {
        "n_searches": c["n"],
        "miss_recall": _rate(c["misses_flagged"], c["true_misses"]),
        "false_alarm_rate": _rate(c["hits_flagged"], c["true_hits"]),
        "rescuable_vs_dead_acc": _rate(c["dead_correct"], c["dead_scored"]),
        "counts": {k: c[k] for k in ("true_misses", "misses_flagged", "true_hits", "hits_flagged",
                                     "dead_scored", "dead_correct", "unjoined")},
        "verdicts": dict(Counter(v for _, v in joined)),
    }


def paired_flips(ours: dict[str, str], base: dict[str, str]) -> dict:
    """Grade transitions base -> ours on questions both finished. Keys like 'wrong->correct'."""
    both = sorted(set(ours) & set(base))
    t = Counter(f"{base[q]}->{ours[q]}" for q in both)
    return {"n_both": len(both), **{f"{a}->{b}": t[f"{a}->{b}"] for a in GRADES for b in GRADES if a != b},
            "unchanged": sum(t[f"{g}->{g}"] for g in GRADES)}


# ---------------------------------------------------------------- io

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _engines() -> list[str]:
    from evals.deps import ENGINES
    return list(ENGINES)


async def _grade_all(items, questions) -> dict[tuple[str, str], str]:
    from evals.judge import grade_answer
    sem = asyncio.Semaphore(int(os.getenv("LLM_MAX_CONCURRENCY", 8)))

    async def one(key, answer):
        async with sem:
            return key, await grade_answer(questions[key[0]], answer or {"final": "", "status": "NOT_FOUND"})

    return dict(await asyncio.gather(*(one(k, a) for k, a in items)))


async def score(log_dir: Path = LOG_DIR, out_dir: Path = OUT) -> dict:
    eventlog = load_eventlog()
    questions = {q.id: q for q in load_questions(split="test") + [t for t in load_traps() if t.split == "test"]}
    manifest_path = Path(out_dir) / MANIFEST.name
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if manifest.get("test_ids") or manifest.get("trap_ids"):
        scope = set(manifest.get("test_ids", [])) | set(manifest.get("trap_ids", []))
    else:
        scope = set(questions)
    runs = {k: p for k, p in finished_runs(log_dir).items() if k[0] in scope and k[0] in questions}

    recs: dict[tuple[str, str], dict] = {}
    checks: list[tuple[str, str]] = []
    missing_answer = []
    for (qid, setup), path in sorted(runs.items()):
        events = eventlog.read_log(path)
        m = log_metrics(events, eventlog.summarize(path))
        if m["answer"] is None:
            missing_answer.append(f"{setup}/{qid}")
        recs[(qid, setup)] = m
        if setup == "second_opinion" and not questions[qid].is_trap:
            checks += [(qid, v) for v in first_engine_checks(events)]

    grades = await _grade_all([(k, r["answer"]) for k, r in recs.items()], questions)
    for k, g in grades.items():
        recs[k]["grade"] = g

    from evals.judge import SPEND
    n_test_scope = len([q for q in scope if q in questions and not questions[q].is_trap])
    n_trap_scope = len([q for q in scope if q in questions and questions[q].is_trap])
    setups, by_setup = {}, {}
    for s in SETUPS:
        test_recs = [r for (q, ss), r in recs.items() if ss == s and not questions[q].is_trap]
        trap_recs = [r for (q, ss), r in recs.items() if ss == s and questions[q].is_trap]
        setups[s] = setup_block(test_recs, trap_recs)
        by_setup[s] = {q: r["grade"] for (q, ss), r in recs.items() if ss == s and not questions[q].is_trap}

    matrix_path = Path(out_dir) / MATRIX.name
    matrix = {(r["question_id"], r["engine"]): bool(r.get("hit")) for r in _read_jsonl(matrix_path)}
    checker = checker_metrics(checks, matrix, _engines())
    if not matrix:
        checker["note"] = f"no matrix at {matrix_path}; run python -m evals.matrix first"

    agree_path = Path(out_dir) / AGREEMENT.name
    if agree_path.exists():
        a = json.loads(agree_path.read_text())
        judge_agreement = {"n_checked": int(a.get("n_checked", 0)), "agree": int(a.get("agree", 0))}
    else:
        judge_agreement = {"n_checked": 0, "agree": 0,
                           "note": "no out/judge_agreement.json yet; run python -m evals.handcheck"}

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_test": n_test_scope,
        "n_traps": n_trap_scope,
        "setups": setups,
        "checker": checker,
        "judge_agreement": judge_agreement,
        "paired": {f"second_opinion_vs_{b}": paired_flips(by_setup["second_opinion"], by_setup[b])
                   for b in BASELINES},
        "judge_cost_usd": {
            "grading_this_run": round(SPEND["judge_usd"], 6),
            "grading_calls": SPEND["judge_calls"], "grading_cached": SPEND["judge_cached"],
            "judge_events_in_logs": round(sum(r["judge_cost"] for r in recs.values()), 6),
            "note": "excluded from every setup's cost",
        },
    }
    if manifest.get("note"):
        results["note"] = manifest["note"]
    try:  # the section 6.12 shape, as frozen in so.contracts (extra keys are allowed)
        from so.contracts import Results
    except ImportError:
        pass
    else:
        Results.model_validate(results)
    if missing_answer:
        results["warnings"] = [f"{len(missing_answer)} finished logs had no answer event and were graded "
                               f"declined: {', '.join(missing_answer[:10])}"]
    return results


# ---------------------------------------------------------------- markdown

def pct(k, n) -> str:
    return f"{100 * k / n:.1f}% ({k}/{n})" if n else f"n/a (0/0)"


def usd(x) -> str:
    return "n/a" if x is None else f"${x:.4f}"


def render_md(r: dict) -> str:
    S = [s for s in SETUPS if s in r["setups"]]
    b = r["setups"]
    L = ["# Second Opinion: eval results", ""]
    if r.get("note"):
        L += [f"**Note: {r['note']}.**", ""]
    L += [f"Generated {r['generated_at']}. Test questions in scope: {r['n_test']}. Traps in scope: {r['n_traps']}.",
          "Every number comes from the test split or the traps. Judge cost is excluded from every setup.", ""]
    for w in r.get("warnings", []):
        L += [f"Warning: {w}", ""]
    short = [s for s in S if b[s]["n"] < r["n_test"]]
    if short:
        L += ["Unfinished runs: " + ", ".join(f"{s} scored {b[s]['n']} of {r['n_test']}" for s in short)
              + ". Re-run evals.run_eval to finish them.", ""]

    def row(label, fn):
        return f"| {label} | " + " | ".join(fn(b[s]) for s in S) + " |"

    L += ["## Test questions (traps excluded)", "",
          "| | " + " | ".join(S) + " |", "|---|" + "---|" * len(S),
          row("Questions scored", lambda x: str(x["n"])),
          row("Correct (accuracy)", lambda x: pct(x["correct"], x["n"])),
          row("Wrong (wrong rate)", lambda x: pct(x["wrong"], x["n"])),
          row("Declined (NOT FOUND)", lambda x: pct(x["declined"], x["n"])),
          row("Engine calls per question", lambda x: f"{x['engine_calls_per_q']:.2f} ({x['engine_calls']}/{x['n']})"),
          row("Search cost per question", lambda x: usd(x["cost_per_q_search"])),
          row("Total cost per question", lambda x: f"{usd(x['cost_per_q_total'])} (${x['cost_total']:.3f}/{x['n']})"),
          row("Cost per correct answer", lambda x: f"{usd(x['cost_per_correct'])} (${x['cost_total']:.3f}/{x['correct']})"),
          row("Cost per search call", lambda x: f"{usd(x['cost_per_search_call'])} "
                                                f"(${x['search_call_cost']:.3f}/{x['search_calls']} calls)"),
          row("Latency p50", lambda x: f"{x['latency_p50_ms'] / 1000:.1f}s (n={x['n']})"),
          row("Latency p95", lambda x: f"{x['latency_p95_ms'] / 1000:.1f}s (n={x['n']})"),
          "",
          "Cost per search call = (engine calls + page reads inside the search tool + checker model) "
          "divided by search tool calls. Total cost includes every agent and checker model call.", "",
          "## Traps (false premise, reported separately)", "",
          "| | " + " | ".join(S) + " |", "|---|" + "---|" * len(S),
          row("Traps scored", lambda x: str(x["trap_n"])),
          row("Confidently wrong", lambda x: pct(x["trap_confident_wrong"], x["trap_n"])),
          row("Flagged the false premise", lambda x: pct(x["trap_correct"], x["trap_n"])),
          row("Declined (NOT FOUND)", lambda x: pct(x["trap_declined"], x["trap_n"])),
          ""]

    c = r["checker"]
    cc = c["counts"]
    L += ["## Checker (second_opinion, first Tavily search of each call vs the hit matrix)", "",
          "| Metric | Value |", "|---|---|",
          f"| Searches joined | {c['n_searches']} (unjoined: {cc['unjoined']}) |",
          f"| Miss recall (true misses labelled not-HIT) | {pct(cc['misses_flagged'], cc['true_misses'])} |",
          f"| False alarm rate (true hits labelled not-HIT) | {pct(cc['hits_flagged'], cc['true_hits'])} |",
          f"| Rescuable vs DEAD accuracy (on true misses) | {pct(cc['dead_correct'], cc['dead_scored'])} |",
          "",
          "Truth label: the matrix row for (question, tavily), which used the question text as the query. "
          "The agent's query may differ, so this is an approximation. Traps are excluded.", ""]
    if c.get("note"):
        L += [f"Note: {c['note']}.", ""]

    L += ["## Paired comparison (test questions both setups finished)", "",
          "| second_opinion vs | Both finished | Wrong to correct | Correct to wrong | Wrong to declined "
          "| Declined to correct | Correct to declined | Declined to wrong | Unchanged |",
          "|---|---|---|---|---|---|---|---|---|"]
    for base in BASELINES:
        p = r["paired"][f"second_opinion_vs_{base}"]
        L.append(f"| {base} | {p['n_both']} | {p['wrong->correct']} | {p['correct->wrong']} | "
                 f"{p['wrong->declined']} | {p['declined->correct']} | {p['correct->declined']} | "
                 f"{p['declined->wrong']} | {p['unchanged']} |")
    L += ["", "Read each row as: the baseline's grade, then second_opinion's grade on the same question.", ""]

    ja = r["judge_agreement"]
    L += ["## Judge", "",
          f"- Hand check agreement: {pct(ja['agree'], ja['n_checked'])}" + (f" ({ja['note']})" if ja.get("note") else ""),
          f"- Judge spend for grading this run: ${r['judge_cost_usd']['grading_this_run']:.4f} "
          f"({r['judge_cost_usd']['grading_calls']} calls, {r['judge_cost_usd']['grading_cached']} cached). "
          "Not included in any setup's cost.", ""]
    return "\n".join(L)


def main(argv=None) -> None:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)
    results = asyncio.run(score())
    OUT.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON.write_text(json.dumps(results, indent=2))
    md = render_md(results)
    RESULTS_MD.write_text(md)
    print(md)
    print(f"wrote {RESULTS_JSON} and {RESULTS_MD}")


if __name__ == "__main__":
    main()
