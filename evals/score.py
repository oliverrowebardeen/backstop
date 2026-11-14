"""Score the eval (lane S4): out/results.json (section 6.12) and out/results.md.

  python -m evals.score

Reads every finished log in out/logs/ (a log with run_end) through so.eventlog,
grades the final answer with evals.judge.grade_answer (cached on disk), and
totals costs from the log. With SO_FAKE=1 everything is read from and written
to out/fake/.

Definitions (sections 6.12 and 7):
- Setup blocks cover test, non-trap questions. Traps are reported separately.
- accuracy = correct / n, wrong_rate = wrong / n (n = questions scored).
- Costs come from so.eventlog.summarize (the run_end totals), minus any llm
  event with role "judge". Judge spend is reported on its own and never counted
  in a setup.
- cost_per_correct = total cost / correct answers.
- cost_per_search_call = (search cost + checker llm cost) / search tool calls.
  Search cost is every `search` event plus every `fetch` inside a search tool
  call (between a search and its search_done, i.e. Second Opinion's page-read
  rescue). The agent's own read_page fetches are not search calls. Search tool
  calls = number of search_done events.
- Extra keys beside the 6.12 fields: n, cost_per_q_checker (checker llm cost per
  question, used by S3's frontier), searches_per_q, and the raw totals.
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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from data.load import load_questions, load_traps
from evals.deps import OUT
from evals.run_eval import LOG_DIR, MANIFEST, SETUPS, finished_runs
from so import eventlog
from so.config import ENGINES, FIRST_ENGINE, LLM_MAX_CONCURRENCY
from so.contracts import Results

GRADES = ("correct", "wrong", "declined")
OURS = "second_opinion"
BASELINES = [s for s in SETUPS if s != OURS]
RESULTS_JSON = OUT / "results.json"
RESULTS_MD = OUT / "results.md"
MATRIX = OUT / "matrix.jsonl"
AGREEMENT = OUT / "judge_agreement.json"
assert OURS in SETUPS


# ---------------------------------------------------------------- per log

def _cost(event: dict) -> float:
    return float(event["data"].get("cost_usd") or 0)


def _llm_cost(events: list[dict], role: str) -> float:
    return sum(_cost(e) for e in events if e["type"] == "llm" and e["data"].get("role") == role)


def search_call_cost(events: list[dict]) -> tuple[float, int]:
    """(search + in-call fetch + checker llm cost, number of search tool calls)."""
    cost, calls, in_call = 0.0, 0, False
    for e in events:
        t = e["type"]
        if t == "search":
            in_call = True
            cost += _cost(e)
        elif t == "fetch" and in_call:
            cost += _cost(e)
        elif t == "search_done":
            in_call = False
            calls += 1
    return cost + _llm_cost(events, "checker"), calls


def first_engine_checks(events: list[dict], first_engine: str = FIRST_ENGINE) -> list[str]:
    """Verdicts of the first check after each search whose engines == [first_engine]."""
    out, pending = [], False
    for e in events:
        if e["type"] == "search":
            pending = list(e["data"].get("engines") or []) == [first_engine]
        elif e["type"] == "check" and pending:
            out.append(str(e["data"].get("verdict")))
            pending = False
    return out


def log_metrics(events: list[dict], summary: dict) -> dict:
    """Costs, calls and latency of one finished run (summary = so.eventlog.summarize), judge excluded."""
    judge = _llm_cost(events, "judge")
    sc_cost, sc_calls = search_call_cost(events)
    return {
        "engine_calls": float(summary["engine_calls"]),
        "cost_search": float(summary["cost_usd_search"]),
        "cost_checker": _llm_cost(events, "checker"),
        "cost_total": float(summary["cost_usd_total"]) - judge,
        "judge_cost": judge,
        "latency_ms": float(summary["latency_ms"]),
        "search_call_cost": sc_cost,
        "search_calls": sc_calls,
        "answer": next((e["data"] for e in events if e["type"] == "answer"), None),
    }


# ---------------------------------------------------------------- aggregates

def _rate(k, n) -> float:
    return round(k / n, 4) if n else 0.0


def _per(total, n, digits=6):
    return round(total / n, digits) if n else None


def setup_block(test_recs: list[dict], trap_recs: list[dict]) -> dict:
    """One section 6.12 setup block (plus extra keys). Each record: log_metrics(...) plus 'grade'."""
    n = len(test_recs)
    g = Counter(r["grade"] for r in test_recs)
    tg = Counter(r["grade"] for r in trap_recs)
    tot = {k: sum(r[k] for r in test_recs) for k in
           ("engine_calls", "cost_search", "cost_checker", "cost_total", "search_call_cost", "search_calls")}
    lat = [r["latency_ms"] for r in test_recs]
    return {
        "n": n,
        "correct": g["correct"], "wrong": g["wrong"], "declined": g["declined"],
        "accuracy": _rate(g["correct"], n), "wrong_rate": _rate(g["wrong"], n),
        "engine_calls": int(tot["engine_calls"]),
        "engine_calls_per_q": _per(tot["engine_calls"], n, 3) or 0.0,
        "searches_per_q": _per(tot["search_calls"], n, 3) or 0.0,
        "cost_search": round(tot["cost_search"], 6),
        "cost_checker": round(tot["cost_checker"], 6),
        "cost_total": round(tot["cost_total"], 6),
        "cost_per_q_search": _per(tot["cost_search"], n) or 0.0,
        "cost_per_q_checker": _per(tot["cost_checker"], n) or 0.0,
        "cost_per_q_total": _per(tot["cost_total"], n) or 0.0,
        "cost_per_correct": _per(tot["cost_total"], g["correct"]),
        "search_calls": tot["search_calls"],
        "search_call_cost": round(tot["search_call_cost"], 6),
        "cost_per_search_call": _per(tot["search_call_cost"], tot["search_calls"]),
        "latency_p50_ms": round(float(np.percentile(lat, 50))) if lat else 0,
        "latency_p95_ms": round(float(np.percentile(lat, 95))) if lat else 0,
        "trap_n": len(trap_recs),
        "trap_confident_wrong": tg["wrong"],
        "trap_correct": tg["correct"],
        "trap_declined": tg["declined"],
    }


def checker_metrics(joined: list[tuple[str, str]], matrix: dict[tuple[str, str], bool],
                    engines: list[str] = ENGINES, first_engine: str = FIRST_ENGINE) -> dict:
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
    both = set(ours) & set(base)
    t = Counter(f"{base[q]}->{ours[q]}" for q in both)
    return {"n_both": len(both), **{f"{a}->{b}": t[f"{a}->{b}"] for a in GRADES for b in GRADES if a != b},
            "unchanged": sum(t[f"{g}->{g}"] for g in GRADES)}


# ---------------------------------------------------------------- io

def _read_matrix(path: Path) -> dict[tuple[str, str], bool]:
    if not path.exists():
        return {}
    rows = (json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return {(r["question_id"], r["engine"]): bool(r["hit"]) for r in rows}


async def _grade_all(items, questions) -> dict[tuple[str, str], str]:
    from evals.judge import grade_answer
    sem = asyncio.Semaphore(LLM_MAX_CONCURRENCY)

    async def one(key, answer):
        async with sem:
            return key, await grade_answer(questions[key[0]], answer or {"final": "", "status": "NOT_FOUND"})

    return dict(await asyncio.gather(*(one(k, a) for k, a in items)))


async def score(log_dir: Path = LOG_DIR, out_dir: Path = OUT) -> dict:
    from evals.judge import SPEND
    questions = {q.id: q for q in load_questions(split="test") + [t for t in load_traps() if t.split == "test"]}
    manifest_path = Path(out_dir) / MANIFEST.name
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    scope = set(manifest.get("test_ids", [])) | set(manifest.get("trap_ids", [])) or set(questions)
    scope &= set(questions)
    runs = {k: p for k, p in finished_runs(log_dir).items() if k[0] in scope}

    recs: dict[tuple[str, str], dict] = {}
    checks: list[tuple[str, str]] = []
    for (qid, setup), path in sorted(runs.items()):
        events = eventlog.read_log(path)
        recs[(qid, setup)] = log_metrics(events, eventlog.summarize(path))
        if setup == OURS and not questions[qid].is_trap:
            checks += [(qid, v) for v in first_engine_checks(events)]
    missing_answer = sorted(f"{s}/{q}" for (q, s), r in recs.items() if r["answer"] is None)

    grades = await _grade_all([(k, r["answer"]) for k, r in recs.items()], questions)
    for k, g in grades.items():
        recs[k]["grade"] = g

    setups, by_setup = {}, {}
    for s in SETUPS:
        mine = {q: r for (q, ss), r in recs.items() if ss == s}
        setups[s] = setup_block([r for q, r in mine.items() if not questions[q].is_trap],
                                [r for q, r in mine.items() if questions[q].is_trap])
        by_setup[s] = {q: r["grade"] for q, r in mine.items() if not questions[q].is_trap}

    matrix_path = Path(out_dir) / MATRIX.name
    matrix = _read_matrix(matrix_path)
    checker = checker_metrics(checks, matrix)
    if not matrix:
        checker["note"] = f"no matrix at {matrix_path}; run python -m evals.matrix first"

    agree_path = Path(out_dir) / AGREEMENT.name
    if agree_path.exists():
        a = json.loads(agree_path.read_text())
        judge_agreement = {"n_checked": int(a["n_checked"]), "agree": int(a["agree"]),
                           "labeler": a.get("labeler", "human")}
    else:
        judge_agreement = {"n_checked": 0, "agree": 0,
                           "note": "no out/judge_agreement.json yet; run python -m evals.handcheck"}

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_test": sum(not questions[q].is_trap for q in scope),
        "n_traps": sum(questions[q].is_trap for q in scope),
        "setups": setups,
        "checker": checker,
        "judge_agreement": judge_agreement,
        "paired": {f"{OURS}_vs_{b}": paired_flips(by_setup[OURS], by_setup[b]) for b in BASELINES},
        "judge_cost_usd": {
            "grading_this_run": round(SPEND["judge_usd"], 6),
            "grading_calls": SPEND["judge_calls"], "grading_cached": SPEND["judge_cached"],
            "judge_events_in_logs": round(sum(r["judge_cost"] for r in recs.values()), 6),
            "note": "excluded from every setup's cost",
        },
    }
    contam_path = Path(out_dir) / "eval_contamination.jsonl"
    if contam_path.exists():
        latest = {}
        for line in contam_path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                latest[(row["question_id"], row["setup"])] = row
        results["contamination"] = {
            setup: {"results_seen": sum(r["results"] for k, r in latest.items() if k in runs and k[1] == setup),
                    "benchmark_copies_removed": sum(r["removed"] for k, r in latest.items()
                                                    if k in runs and k[1] == setup)}
            for setup in setups}
    if manifest.get("note"):
        results["note"] = manifest["note"]
    if missing_answer:
        results["warnings"] = [f"{len(missing_answer)} finished logs had no answer event and were graded "
                               f"declined: {', '.join(missing_answer[:10])}"]
    Results.model_validate(results)  # the section 6.12 shape as frozen in so.contracts
    return results


# ---------------------------------------------------------------- markdown

def pct(k, n) -> str:
    return f"{100 * k / n:.1f}% ({k}/{n})" if n else "n/a (0/0)"


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

    def table_head():
        return ["| | " + " | ".join(S) + " |", "|---|" + "---|" * len(S)]

    L += ["## Test questions (traps excluded)", "", *table_head(),
          row("Questions scored", lambda x: str(x["n"])),
          row("Correct (accuracy)", lambda x: pct(x["correct"], x["n"])),
          row("Wrong (wrong rate)", lambda x: pct(x["wrong"], x["n"])),
          row("Declined (NOT FOUND)", lambda x: pct(x["declined"], x["n"])),
          row("Search tool calls per question", lambda x: f"{x['searches_per_q']:.2f} ({x['search_calls']}/{x['n']})"),
          row("Engine calls per question", lambda x: f"{x['engine_calls_per_q']:.2f} ({x['engine_calls']}/{x['n']})"),
          row("Search cost per question", lambda x: f"{usd(x['cost_per_q_search'])} (${x['cost_search']:.3f}/{x['n']})"),
          row("Checker cost per question", lambda x: f"{usd(x['cost_per_q_checker'])} (${x['cost_checker']:.3f}/{x['n']})"),
          row("Total cost per question", lambda x: f"{usd(x['cost_per_q_total'])} (${x['cost_total']:.3f}/{x['n']})"),
          row("Cost per correct answer", lambda x: f"{usd(x['cost_per_correct'])} (${x['cost_total']:.3f}/{x['correct']})"),
          row("Cost per search call", lambda x: f"{usd(x['cost_per_search_call'])} "
                                                f"(${x['search_call_cost']:.3f}/{x['search_calls']} calls)"),
          row("Latency p50", lambda x: f"{x['latency_p50_ms'] / 1000:.1f}s (n={x['n']})"),
          row("Latency p95", lambda x: f"{x['latency_p95_ms'] / 1000:.1f}s (n={x['n']})"),
          "",
          "Cost per search call = (engine calls + page reads inside the search tool + checker model) "
          "divided by search tool calls. Total cost includes every agent and checker model call.", "",
          "## Traps (false premise, reported separately)", "", *table_head(),
          row("Traps scored", lambda x: str(x["trap_n"])),
          row("Confidently wrong", lambda x: pct(x["trap_confident_wrong"], x["trap_n"])),
          row("Flagged the false premise", lambda x: pct(x["trap_correct"], x["trap_n"])),
          row("Declined (NOT FOUND)", lambda x: pct(x["trap_declined"], x["trap_n"])),
          ""]

    c, cc = r["checker"], r["checker"]["counts"]
    L += [f"## Checker ({OURS}, first {FIRST_ENGINE} search of each call vs the hit matrix)", "",
          "| Metric | Value |", "|---|---|",
          f"| Searches joined | {c['n_searches']} (unjoined: {cc['unjoined']}) |",
          f"| Miss recall (true misses labelled not-HIT) | {pct(cc['misses_flagged'], cc['true_misses'])} |",
          f"| False alarm rate (true hits labelled not-HIT) | {pct(cc['hits_flagged'], cc['true_hits'])} |",
          f"| Rescuable vs DEAD accuracy (on true misses) | {pct(cc['dead_correct'], cc['dead_scored'])} |",
          "",
          f"Truth label: the matrix row for (question, {FIRST_ENGINE}), which used the question text as the "
          "query. The agent's query may differ, so this is an approximation. Traps are excluded.", ""]
    if c.get("note"):
        L += [f"Note: {c['note']}.", ""]

    L += ["## Paired comparison (test questions both setups finished)", "",
          f"| {OURS} vs | Both finished | Wrong to correct | Correct to wrong | Wrong to declined "
          "| Declined to correct | Correct to declined | Declined to wrong | Unchanged |",
          "|---|---|---|---|---|---|---|---|---|"]
    for base in BASELINES:
        p = r["paired"][f"{OURS}_vs_{base}"]
        L.append(f"| {base} | {p['n_both']} | {p['wrong->correct']} | {p['correct->wrong']} | "
                 f"{p['wrong->declined']} | {p['declined->correct']} | {p['correct->declined']} | "
                 f"{p['declined->wrong']} | {p['unchanged']} |")
    L += ["", f"Read each row as: the baseline's grade, then {OURS}'s grade on the same question.", ""]

    if r.get("contamination"):
        L += ["## Benchmark contamination filter", "",
              "Search results that copy the benchmark (dataset viewers, published answer sheets, pages "
              "quoting the question) were removed before any setup, checker or agent saw them "
              "(evals/contamination.py, installed through so.telem_client.RESULT_FILTER).", "",
              "| Setup | Results seen | Removed |", "|---|---|---|"]
        for setup in S:
            c = r["contamination"].get(setup, {})
            L.append(f"| {setup} | {c.get('results_seen', 0)} | "
                     f"{pct(c.get('benchmark_copies_removed', 0), c.get('results_seen', 0))} |")
        L.append("")
    ja, jc = r["judge_agreement"], r["judge_cost_usd"]
    labeler = ja.get("labeler", "human")
    L += ["## Judge", "",
          f"- Hand check agreement: {pct(ja['agree'], ja['n_checked'])}, labeled by {labeler}"
          + (f" ({ja['note']})" if ja.get("note") else ""),
          f"- Judge spend for grading this run: ${jc['grading_this_run']:.4f} "
          f"({jc['grading_calls']} calls, {jc['grading_cached']} cached). Not included in any setup's cost.", ""]
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
