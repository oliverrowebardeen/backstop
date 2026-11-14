"""Score the eval (lane S4): out/results.json (section 6.12) and out/results.md.

  python -m evals.score

Reads every finished log in out/logs/ (a log with run_end) through so.eventlog,
grades the final answer with evals.judge.grade_answer (cached on disk), and
totals costs from the log.

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
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from data.load import load_questions, load_traps
from evals import deps
from evals.retrieval import checker_metrics as offline_checker_metrics
from evals.retrieval import coverage_by_setup, judge_runs
from evals.run_eval import MANIFEST_NAME, SETUPS, default_log_dir, finished_runs
from so import eventlog
from so.config import ENGINES, FIRST_ENGINE, LLM_MAX_CONCURRENCY
from so.contracts import Results

GRADES = ("correct", "wrong", "declined")
OURS = "second_opinion"
BASELINES = [s for s in SETUPS if s != OURS]
RESULTS_JSON = "results.json"
RESULTS_MD = "results.md"
AGREEMENT = "judge_agreement.json"
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


def paired_flips(ours: dict[str, str], base: dict[str, str]) -> dict:
    """Grade transitions base -> ours on questions both finished. Keys like 'wrong->correct'."""
    both = set(ours) & set(base)
    t = Counter(f"{base[q]}->{ours[q]}" for q in both)
    return {"n_both": len(both), **{f"{a}->{b}": t[f"{a}->{b}"] for a in GRADES for b in GRADES if a != b},
            "unchanged": sum(t[f"{g}->{g}"] for g in GRADES)}


# ---------------------------------------------------------------- io

async def _grade_all(items, questions) -> dict[tuple[str, str], str | None]:
    """Grade every answer. A persistent judge failure yields None (excluded and reported), never a label."""
    from evals.judge import JudgeError, grade_answer
    sem = asyncio.Semaphore(LLM_MAX_CONCURRENCY)

    async def one(key, answer):
        async with sem:
            try:
                return key, await grade_answer(questions[key[0]], answer or {"final": "", "status": "NOT_FOUND"})
            except JudgeError as e:
                print(f"  ! could not grade {key[1]}/{key[0]}: {e}", file=sys.stderr)
                return key, None

    return dict(await asyncio.gather(*(one(k, a) for k, a in items)))


def _provenance(runs: dict) -> dict:
    """The configuration behind the reported numbers: models actually used, judge, prices, commit."""
    import subprocess

    from evals.judge import provenance as judge_provenance
    from so import config

    models: dict[str, set[str]] = {}
    for path in runs.values():
        for e in eventlog.read_log(path):
            if e["type"] == "llm" and e["data"].get("model"):
                models.setdefault(e["data"].get("role", "?"), set()).add(e["data"]["model"])
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parent.parent, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        commit = None
    return {"models_in_logs": {role: sorted(m) for role, m in models.items()},
            "judge": judge_provenance(), "engine_price_usd": dict(config.PRICE_USD),
            "default_price_usd": config.DEFAULT_PRICE_USD, "llm_price_usd_per_mtok": config.LLM_PRICE_USD,
            "telem_base_url": config.TELEM_BASE_URL, "scored_at_commit": commit}


async def score(log_dir: Path | None = None, out_dir: Path | None = None) -> dict:
    from evals.judge import SPEND
    log_dir = default_log_dir() if log_dir is None else log_dir
    out_dir = deps.OUT if out_dir is None else out_dir
    questions = {q.id: q for q in load_questions(split="test") + [t for t in load_traps() if t.split == "test"]}
    manifest_path = Path(out_dir) / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    scope = set(manifest.get("test_ids", [])) | set(manifest.get("trap_ids", [])) or set(questions)
    scope &= set(questions)
    runs = {k: p for k, p in finished_runs(log_dir).items() if k[0] in scope}

    recs: dict[tuple[str, str], dict] = {}
    for (qid, setup), path in sorted(runs.items()):
        events = eventlog.read_log(path)
        recs[(qid, setup)] = log_metrics(events, eventlog.summarize(path))
    missing_answer = sorted(f"{s}/{q}" for (q, s), r in recs.items() if r["answer"] is None)

    grades = await _grade_all([(k, r["answer"]) for k, r in recs.items()], questions)
    ungraded = sorted(f"{s}/{q}" for (q, s), g in grades.items() if g is None)
    for k, g in grades.items():
        if g is None:
            recs.pop(k)  # excluded from every metric; counted in warnings below
        else:
            recs[k]["grade"] = g

    setups, by_setup = {}, {}
    for s in SETUPS:
        mine = {q: r for (q, ss), r in recs.items() if ss == s}
        setups[s] = setup_block([r for q, r in mine.items() if not questions[q].is_trap],
                                [r for q, r in mine.items() if questions[q].is_trap])
        by_setup[s] = {q: r["grade"] for q, r in mine.items() if not questions[q].is_trap}

    # Offline truth for every logged search, judged on the exact list each search returned.
    graded_test = {k: runs[k] for k in recs if not questions[k[0]].is_trap}
    truth = await judge_runs(graded_test, questions)
    checker = offline_checker_metrics(truth)
    present = [s for s in SETUPS if any(ss == s for (_, ss) in graded_test)]
    cohort = set.intersection(*[{q for (q, ss) in graded_test if ss == s} for s in present]) if present else set()
    coverage = coverage_by_setup(truth, {s: cohort for s in present})
    for s in present:
        mine = [recs[(q, s)] for q in cohort]
        n = len(mine) or 1
        coverage[s]["cost_per_q_search"] = round(sum(r["cost_search"] for r in mine) / n, 6)
        coverage[s]["cost_per_q_checker"] = round(sum(r["cost_checker"] for r in mine) / n, 6)
    retrieval = {"cohort_n": len(cohort), "cohort_ids": sorted(cohort), "setups": coverage,
                 "metric": "answer found in results: any search in the agent's run surfaced the gold answer "
                           "(offline judge on the exact, contamination-filtered result lists)",
                 "cohort": "test questions every setup finished and the judge graded"}

    agree_path = Path(out_dir) / AGREEMENT
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
        "retrieval": retrieval,
        "provenance": _provenance(runs),
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
    warnings = []
    if missing_answer:
        warnings.append(f"{len(missing_answer)} finished logs had no answer event and were graded "
                        f"declined: {', '.join(missing_answer[:10])}")
    if ungraded:
        warnings.append(f"{len(ungraded)} answers could not be graded (judge returned no valid verdict) "
                        f"and are excluded from every metric: {', '.join(ungraded[:10])}")
    if warnings:
        results["warnings"] = warnings
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

    c = r["checker"]
    L += [f"## Checker ({OURS}, every check vs the exact list it saw)", "",
          "| Metric | Value |", "|---|---|",
          f"| Checked searches judged | {c['n_searches']} (not in cache: {c.get('searches_not_in_cache', 0)}, "
          f"judge errors: {c.get('judge_errors', 0)}) |",
          f"| Miss recall (true misses labelled not-HIT) | "
          f"{pct(round(c['miss_recall'] * c.get('n_true_misses', 0)), c.get('n_true_misses', 0))} |",
          f"| False alarm rate (true hits labelled not-HIT) | "
          f"{pct(round(c['false_alarm_rate'] * c.get('n_true_hits', 0)), c.get('n_true_hits', 0))} |",
          f"| Rescuable vs DEAD accuracy (on true misses) | "
          f"{pct(round(c['rescuable_vs_dead_acc'] * c.get('n_true_misses', 0)), c.get('n_true_misses', 0))} |",
          "", f"Truth: {c.get('method', 'offline judge')}. Traps are excluded.", ""]
    rt = r.get("retrieval") or {}
    if rt.get("setups"):
        L += [f"## Retrieval coverage of the agent runs (common cohort: {rt['cohort_n']} test questions)", "",
              "| Setup | Answer found in results | Search cost / q | Checker cost / q |", "|---|---|---|---|"]
        for s in S:
            v = rt["setups"].get(s)
            if v:
                found = pct(v["covered"], v["n_questions"]) if v["coverage"] is not None else "not judged"
                L.append(f"| {s} | {found} | {usd(v['cost_per_q_search'])} | "
                         f"{usd(v['cost_per_q_checker'])} |")
        L += ["", f"Metric: {rt['metric']}.", ""]
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
    out = deps.OUT
    out.mkdir(parents=True, exist_ok=True)
    (out / RESULTS_JSON).write_text(json.dumps(results, indent=2))
    md = render_md(results)
    (out / RESULTS_MD).write_text(md)
    print(md)
    print(f"wrote {out / RESULTS_JSON} and {out / RESULTS_MD}")


if __name__ == "__main__":
    main()
