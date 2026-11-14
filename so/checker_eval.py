"""Score the checker against the hit matrix on the learn split.

    python -m so.checker_eval --split learn

Truth comes from out/matrix.jsonl: whether an engine's top 5 contained the gold
answer, as decided by the judge. The checker sees the same top 5 the judge saw
(out/matrix_results.jsonl), the same query and goal the matrix used (the
question text) and today's date. It never sees gold answers.

Prints miss recall, false alarm rate, rescuable-vs-DEAD accuracy and a
confusion table of verdicts. Metric definitions match evals/score.py.

Only the learn split is allowed. Every reported number comes from the test
split, so tuning the checker on it would leak.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from so import checker
from so.config import ENGINES, FIRST_ENGINE, ROOT
from so.contracts import CheckerVerdict, MatrixRow, Question, Result

OUT = ROOT / "out"
MATRIX = OUT / "matrix.jsonl"
RESULTS = OUT / "matrix_results.jsonl"
TOP_K = 5
TARGET_MISS_RECALL = 0.85
TARGET_FALSE_ALARM = 0.25
ALLOWED_SPLIT = "learn"


class SplitRefused(ValueError):
    """Raised when anything other than the learn split reaches the checker eval."""


@dataclass
class Case:
    """One (question, engine) search: what the checker sees plus the matrix truth."""

    question: Question
    engine: str
    results: list[Result]
    truth_hit: bool
    others_hit: list[bool] = field(default_factory=list)


@dataclass
class Scored:
    case: Case
    verdict: CheckerVerdict


# Loading

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSONL, skipping blank lines and a half-written last line."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def learn_questions(questions: Sequence[Question] | None = None) -> list[Question]:
    """The learn-split questions. Raises if any other split is present."""
    if questions is None:
        from data.load import load_questions

        questions = load_questions(split=ALLOWED_SPLIT)
    questions = list(questions)
    leaked = [q.id for q in questions if q.split != ALLOWED_SPLIT or q.is_trap]
    if leaked:
        raise SplitRefused(f"checker_eval only reads learn questions; got {leaked[:5]}")
    return questions


def load_truth(path: Path, qids: set[str]) -> dict[tuple[str, str], MatrixRow]:
    """Matrix rows for the given question ids only; rows for other questions are never kept."""
    truth = {}
    for row in read_jsonl(path):
        if row.get("question_id") in qids:
            truth[(row["question_id"], row["engine"])] = MatrixRow.model_validate(row)
    return truth


def load_results(path: Path, qids: set[str]) -> dict[tuple[str, str], list[Result]]:
    """The top 5 results the judge saw, per (question, engine). A later line replaces an earlier one."""
    out = {}
    for row in read_jsonl(path):
        if row.get("question_id") in qids:
            out[(row["question_id"], row["engine"])] = [Result.model_validate(r) for r in row["results"][:TOP_K]]
    return out


def build_cases(questions: Sequence[Question], truth: dict[tuple[str, str], MatrixRow],
                results: dict[tuple[str, str], list[Result]],
                engines: Sequence[str]) -> tuple[list[Case], list[tuple[Question, str]]]:
    """Join truth with results. Returns the cases and the (question, engine) pairs lacking results."""
    cases, missing = [], []
    for q in questions:
        for engine in engines:
            row = truth.get((q.id, engine))
            if row is None:
                continue
            if (q.id, engine) not in results:
                missing.append((q, engine))
                continue
            others = [truth[(q.id, e)].hit for e in ENGINES if e != engine and (q.id, e) in truth]
            cases.append(Case(q, engine, results[(q.id, engine)], row.hit, others))
    return cases, missing


async def fill_missing(missing: Sequence[tuple[Question, str]]) -> dict[tuple[str, str], list[Result]]:
    """Fetch absent results with the matrix's own call, so it is usually a Telem cache hit."""
    from so import telem_client

    async def one(q: Question) -> dict[str, Any]:
        return await telem_client.search_engines(q.question, ENGINES, num_results=TOP_K, goal=q.question)

    by_q = {q.id: q for q, _ in missing}
    runs = dict(zip(by_q, await asyncio.gather(*(one(q) for q in by_q.values()))))
    filled = {}
    for q, engine in missing:
        run = runs[q.id].get(engine)
        if run is not None and run.status == "ok":
            filled[(q.id, engine)] = list(run.results[:TOP_K])
    return filled


# Scoring

async def score(cases: Sequence[Case], today: date | str) -> list[Scored]:
    verdicts = await asyncio.gather(*(
        checker.check(c.question.question, c.question.question, c.results, today) for c in cases))
    return [Scored(c, v) for c, v in zip(cases, verdicts)]


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def metrics(scored: Sequence[Scored]) -> dict[str, Any]:
    """Miss recall, false alarm rate and rescuable-vs-DEAD accuracy, as defined in evals/score.py."""
    c: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = {"hit": Counter(), "miss": Counter()}
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    for s in scored:
        flagged = s.verdict.verdict != "HIT"
        truth = "hit" if s.case.truth_hit else "miss"
        confusion[truth][s.verdict.verdict] += 1
        cat = by_category[s.case.question.category]
        if s.case.truth_hit:
            c["true_hits"] += 1
            c["hits_flagged"] += flagged
            cat["true_hits"] += 1
            cat["hits_flagged"] += flagged
        else:
            c["true_misses"] += 1
            c["misses_flagged"] += flagged
            cat["true_misses"] += 1
            cat["misses_flagged"] += flagged
            if s.case.others_hit:
                c["dead_scored"] += 1
                c["dead_correct"] += (s.verdict.verdict == "DEAD") == (not any(s.case.others_hit))
    usage = [s.verdict for s in scored if isinstance(s.verdict, checker.CheckedVerdict)]
    latencies = [v.latency_ms for v in usage if v.model and not v.cached]
    return {
        "split": ALLOWED_SPLIT,
        "n_searches": len(scored),
        "true_hits": c["true_hits"],
        "true_misses": c["true_misses"],
        "miss_recall": _rate(c["misses_flagged"], c["true_misses"]),
        "false_alarm_rate": _rate(c["hits_flagged"], c["true_hits"]),
        "hit_precision": _rate(confusion["hit"]["HIT"], confusion["hit"]["HIT"] + confusion["miss"]["HIT"]),
        "rescuable_vs_dead_acc": _rate(c["dead_correct"], c["dead_scored"]),
        "dead_scored": c["dead_scored"],
        "confusion": {k: {v: confusion[k][v] for v in checker.VERDICTS} for k in confusion},
        "by_category": {k: {"miss_recall": _rate(v["misses_flagged"], v["true_misses"]),
                            "false_alarm_rate": _rate(v["hits_flagged"], v["true_hits"]),
                            "true_hits": v["true_hits"], "true_misses": v["true_misses"]}
                        for k, v in sorted(by_category.items())},
        "checker_errors": sum(s.verdict.why.startswith("Checker error") for s in scored),
        "cost_usd": round(sum(v.cost_usd for v in usage), 6),
        "cost_per_check": round(sum(v.cost_usd for v in usage) / len(usage), 6) if usage else 0.0,
        "latency_p50_ms": round(statistics.median(latencies)) if latencies else None,
        "cached_checks": sum(v.cached for v in usage),
    }


def report(m: dict[str, Any]) -> str:
    ok = m["miss_recall"] >= TARGET_MISS_RECALL and m["false_alarm_rate"] <= TARGET_FALSE_ALARM
    lines = [
        f"Checker on the {m['split']} split: {m['n_searches']} searches "
        f"({m['true_misses']} true misses, {m['true_hits']} true hits)",
        "",
        f"  miss recall            {m['miss_recall']:.3f}   (target >= {TARGET_MISS_RECALL})",
        f"  false alarm rate       {m['false_alarm_rate']:.3f}   (target <= {TARGET_FALSE_ALARM})",
        f"  HIT precision          {m['hit_precision']:.3f}",
        f"  rescuable vs DEAD acc  {m['rescuable_vs_dead_acc']:.3f}   (on {m['dead_scored']} true misses)",
        "",
        "  truth \\ verdict " + "".join(f"{v:>14}" for v in checker.VERDICTS),
    ]
    for truth in ("hit", "miss"):
        lines.append(f"  {truth:16}" + "".join(f"{m['confusion'][truth][v]:>14}" for v in checker.VERDICTS))
    lines.append("")
    for cat, v in m["by_category"].items():
        lines.append(f"  {cat:8} miss recall {v['miss_recall']:.3f} ({v['true_misses']} misses), "
                     f"false alarms {v['false_alarm_rate']:.3f} ({v['true_hits']} hits)")
    latency = f"{m['latency_p50_ms']} ms" if m["latency_p50_ms"] is not None else "n/a (all cached)"
    lines += ["",
              f"  checker cost ${m['cost_usd']:.4f} (${m['cost_per_check']:.5f} per check), "
              f"median latency {latency}, {m['cached_checks']} cached, {m['checker_errors']} errors",
              f"  target {'MET' if ok else 'NOT MET'}"]
    return "\n".join(lines)


def disagreements(scored: Sequence[Scored], limit: int) -> str:
    """The cases that cost accuracy (wrong HIT) or money (false alarm), for reading during tuning."""
    wrong_hits = [s for s in scored if not s.case.truth_hit and s.verdict.verdict == "HIT"]
    false_alarms = [s for s in scored if s.case.truth_hit and s.verdict.verdict != "HIT"]
    lines = []
    for title, group in (("Wrong HIT (matrix says miss)", wrong_hits),
                         ("False alarm (matrix says hit)", false_alarms)):
        lines.append(f"\n{title}: {len(group)}")
        for s in group[:limit]:
            lines.append(f"  {s.case.question.id} [{s.case.engine}] {s.verdict.verdict} "
                         f"{s.verdict.confidence:.2f}: {s.case.question.question}\n"
                         f"      checker: {s.verdict.why}")
    return "\n".join(lines)


# Entry point

async def run(split: str, *, engines: Sequence[str], today: date | str, matrix: Path = MATRIX,
              results_path: Path = RESULTS, fill: bool = False, show: int = 0,
              questions: Sequence[Question] | None = None) -> dict[str, Any]:
    if split != ALLOWED_SPLIT:
        raise SplitRefused(f"checker_eval runs on the learn split only, not {split!r}. "
                           "Reported numbers come from the test split, so it is never tuned on.")
    qs = learn_questions(questions)
    qids = {q.id for q in qs}
    truth = load_truth(matrix, qids)
    if not truth:
        raise FileNotFoundError(f"no learn-split rows in {matrix}; run the hit matrix first")
    results = load_results(results_path, qids)
    cases, missing = build_cases(qs, truth, results, engines)
    if missing and fill:
        results.update(await fill_missing(missing))
        cases, missing = build_cases(qs, truth, results, engines)
    if missing:
        print(f"{len(missing)} matrix rows have no stored results and were skipped"
              f"{'' if fill else ' (pass --fill-missing to fetch them through the Telem cache)'}.",
              file=sys.stderr)
    scored = await score(cases, today)
    m = metrics(scored)
    m["engines"] = list(engines)
    m["skipped_no_results"] = len(missing)
    print(report(m))
    if show:
        print(disagreements(scored, show))
    return m


def main(argv: Sequence[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--split", required=True, help="must be 'learn'; any other split is refused")
    p.add_argument("--engines", nargs="+", default=[FIRST_ENGINE],
                   help=f"engines whose results to check (default {FIRST_ENGINE})")
    p.add_argument("--today", default=date.today().isoformat(), help="date shown to the checker")
    p.add_argument("--matrix", type=Path, default=MATRIX)
    p.add_argument("--results", type=Path, default=RESULTS)
    p.add_argument("--fill-missing", action="store_true",
                   help="fetch results absent from the results file with the matrix's Telem call")
    p.add_argument("--show", type=int, default=0, metavar="N", help="print N disagreements of each kind")
    p.add_argument("--json", type=Path, help="also write the metrics to this file")
    a = p.parse_args(argv)
    if a.split != ALLOWED_SPLIT:
        p.exit(2, f"refused: checker_eval runs on the learn split only, not {a.split!r}. "
                  "Reported numbers come from the test split, so it is never tuned on.\n")
    unknown = sorted(set(a.engines) - set(ENGINES))
    if unknown:
        p.error(f"unknown engines {unknown}; choose from {ENGINES}")
    try:
        m = asyncio.run(run(a.split, engines=a.engines, today=a.today, matrix=a.matrix,
                            results_path=a.results, fill=a.fill_missing, show=a.show))
    except (FileNotFoundError, SplitRefused) as exc:
        p.exit(1, f"{exc}\n")
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(m, indent=2) + "\n")


if __name__ == "__main__":
    main()
