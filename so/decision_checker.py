"""Optional Decisions checker and paired learn-only evaluation.

Run ``python -m so.decision_checker --compare --split learn``. The default
Tavily rows match checker_eval; DECISION_EVAL_ENGINES selects other engines.
The search integration remains unchanged. Call ``configured_check`` to opt
into CHECKER_BACKEND, or ``check`` to explicitly use Decisions.

All settings are environment-backed in so.config. Comparisons share its
$3 process budget, require disk caching, and exclude ceramic by default.
Latency uses original recorded call timings, including cached calls.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import Field

from so import checker, config, llm
from so.checker_eval import (
    ALLOWED_SPLIT, Case, Scored, SplitRefused, build_cases, contamination, learn_questions,
    load_results, load_truth, metrics,
)
from so.contracts import CheckerVerdict, LLMResponse, MatrixRow, Result, RunContext

logger = logging.getLogger(__name__)


class DecisionVerdict(checker.CheckedVerdict):
    """Contract verdict with explicit fallback and usage metadata."""

    failure: str | None = Field(default=None, exclude=True)
    fallback: bool = Field(default=False, exclude=True)
    decision_calls: int = Field(default=0, ge=0, exclude=True)


def _instructions() -> tuple[str, dict[str, str]]:
    text = config.DECISION_PROMPT_PATH.read_text()
    instructions, marker, _ = text.partition("## Output")
    if not marker:
        raise ValueError("Checker prompt has no Output section")
    definitions = {}
    for line in instructions.splitlines():
        for verdict in checker.VERDICTS:
            prefix = f"- {verdict}: "
            if line.startswith(prefix):
                definitions[verdict] = line[len(prefix):]
    if set(definitions) != set(checker.VERDICTS):
        raise ValueError("Checker prompt must define all five verdicts")
    return instructions.strip(), definitions


def _sum_usage(left: LLMResponse, right: LLMResponse) -> LLMResponse:
    return LLMResponse(role="checker", model=right.model or left.model,
        tokens_in=left.tokens_in + right.tokens_in, tokens_out=left.tokens_out + right.tokens_out,
        cost_usd=left.cost_usd + right.cost_usd, latency_ms=left.latency_ms + right.latency_ms,
        cached=left.cached and right.cached)


def _usage(verdict: checker.CheckedVerdict) -> LLMResponse:
    return LLMResponse(role="checker", **{name: getattr(verdict, name) for name in (
        "model", "tokens_in", "tokens_out", "cost_usd", "latency_ms", "cached")})


def _with_usage(verdict: CheckerVerdict, usage: LLMResponse, **extra: Any) -> DecisionVerdict:
    return DecisionVerdict(**verdict.model_dump(), **{name: getattr(usage, name) for name in (
        "model", "tokens_in", "tokens_out", "cost_usd", "latency_ms", "cached")}, **extra)


def map_choice(choice: str, confidence: float, results: Sequence[Result]) -> CheckerVerdict:
    """Map a supplied option to the contract without generating URLs or rewrites."""
    if choice in {"STALE", "WRONG_ENTITY", "DEAD"}:
        why = {"STALE": "Evidence predates the requested period.",
               "WRONG_ENTITY": "Results concern a different entity.",
               "DEAD": "Results do not contain relevant evidence."}[choice]
        return CheckerVerdict(verdict=choice, confidence=confidence, why=why, rewrite=None)
    verdict, separator, number = choice.rpartition("_")
    if not separator or verdict not in {"HIT", "SHALLOW"} or not number.isdecimal():
        raise ValueError("Unknown Decisions checker option")
    rank = int(number)
    if not 1 <= rank <= len(results):
        raise ValueError("Decisions selected a result that was not supplied")
    why = (f"Result {rank} states the requested answer." if verdict == "HIT"
           else f"Result {rank} is relevant but its snippet omits the answer.")
    return CheckerVerdict(verdict=verdict, confidence=confidence, why=why,
                          evidence_rank=rank, best_url=results[rank - 1].url, rewrite=None)


async def check(query: str, goal: str, results: Sequence[Result | dict[str, Any]],
                today: date | str | None = None, *, ctx: RunContext | None = None) -> DecisionVerdict:
    """Check the top results, using the ordinary LLM checker only on a timeout."""
    top = checker.as_results(results)[:config.DECISION_TOP_K]
    total = LLMResponse(role="checker", cached=True)
    if not top:
        return _with_usage(CheckerVerdict(verdict="DEAD", confidence=1.0,
            why="The search returned no results."), total)
    instructions, definitions = _instructions()
    evidence = checker.render_user(query, goal, top, today)
    choices = [llm.DecisionChoice(value=f"{verdict}_{rank}",
        description=f"Result {rank}. {definitions[verdict]}")
        for verdict in ("HIT", "SHALLOW") for rank in range(1, len(top) + 1)]
    choices += [llm.DecisionChoice(value=v, description=definitions[v])
                for v in ("STALE", "WRONG_ENTITY", "DEAD")]
    calls = 0

    async def ask(prompt: str, options: Sequence[llm.DecisionChoice]) -> llm.DecisionResponse:
        nonlocal total, calls
        calls += 1
        try:
            response = await llm.decide(prompt, evidence, options)
        except llm.DecisionCallError as exc:
            total = _sum_usage(total, exc.usage)
            raise
        total = _sum_usage(total, response)
        return response

    try:
        if len(choices) <= config.DECISION_MAX_CHOICES:
            response = await ask(instructions + "\nChoose the verdict and, when applicable, its result number.", choices)
            choice, confidence = response.choice, response.confidence
        else:
            response = await ask(instructions, [llm.DecisionChoice(value=v, description=definitions[v])
                                                for v in checker.VERDICTS])
            choice, confidence = response.choice, response.confidence
            if choice in {"HIT", "SHALLOW"}:
                rank = await ask(instructions + f"\nThe verdict is {choice}. Select the supporting result.",
                    [llm.DecisionChoice(value=str(i), description=f"Result {i}") for i in range(1, len(top) + 1)])
                choice = f"{choice}_{rank.choice}"
                # Both conditional selections must hold; use the lower API confidence.
                confidence = min(confidence, rank.confidence)
        verdict = map_choice(choice, confidence, top)
    except llm.DecisionCallError as exc:
        checker._log_usage(ctx, total)
        if exc.kind != "timeout":
            raise llm.DecisionCallError(str(exc), usage=total, kind=exc.kind) from exc
        logger.warning("Decisions timed out; using the LLM checker with its configured provider")
        try:
            fallback = await checker.check(query, goal, top, today, ctx=ctx)
        except config.BudgetExceeded:
            raise
        except checker.CheckerError as fallback_error:
            raise llm.DecisionCallError("Decisions timed out and the LLM fallback failed",
                                       usage=total, kind="fallback_failed") from fallback_error
        return _with_usage(fallback, _sum_usage(total, _usage(fallback)),
                           failure="timeout", fallback=True, decision_calls=calls)
    checker._log_usage(ctx, total)
    return _with_usage(verdict, total, decision_calls=calls)


async def configured_check(query: str, goal: str, results: Sequence[Result | dict[str, Any]],
                           today: date | str | None = None, *, ctx: RunContext | None = None) -> checker.CheckedVerdict:
    """Opt-in dispatcher; the existing checker module and default search are unchanged."""
    backend = check if config.CHECKER_BACKEND == "decision" else checker.check
    return await backend(query, goal, results, today, ctx=ctx)


@dataclass
class Observation:
    case: Case
    truth: MatrixRow
    verdict: checker.CheckedVerdict | None
    usage: LLMResponse
    original_positions: tuple[int, ...]
    error: str | None = None


def summarize(rows: Sequence[Observation], *, threshold: float | None = None) -> dict[str, Any]:
    """Use checker_eval's metric math; add evidence, usage, and failure counts."""
    valid = [r for r in rows if r.verdict is not None and r.error is None]
    scored = []
    for row in valid:
        verdict = row.verdict
        if threshold is not None and verdict.verdict == "HIT" and verdict.confidence < threshold:
            verdict = verdict.model_copy(update={"verdict": "SHALLOW"})
        scored.append(Scored(row.case, verdict))
    report = metrics(scored)
    hits = [r for r in valid if r.truth.hit and r.truth.hit_rank is not None]
    # Both models number by position. The matrix remaps its rank to the unfiltered list.
    agreements = sum(r.verdict.evidence_rank is not None
        and 1 <= r.verdict.evidence_rank <= len(r.case.results)
        and r.original_positions[r.verdict.evidence_rank - 1] == r.truth.hit_rank for r in hits)
    latencies = sorted(r.usage.latency_ms for r in rows if r.usage.model)
    report.update(attempted=len(rows), failed=sum(r.error is not None for r in rows),
        fallbacks=sum(bool(getattr(r.verdict, "fallback", False)) for r in rows),
        unparseable=sum(r.error in {"malformed", "truncated"} for r in rows),
        evidence_rank_agreement=round(agreements / len(hits), 4) if hits else None,
        evidence_rank_agreed=agreements, evidence_rank_count=len(hits),
        latency_p50_ms=round(statistics.median(latencies), 2) if latencies else None,
        latency_p90_ms=round(latencies[max(0, math.ceil(len(latencies) * .9) - 1)], 2) if latencies else None,
        latency_count=len(latencies), latency_source="original call timings, including disk cache",
        cost_usd=round(sum(r.usage.cost_usd for r in rows), 8),
        cost_per_check=round(sum(r.usage.cost_usd for r in rows) / len(rows), 8) if rows else None,
        cached_checks=sum(r.usage.cached for r in rows),
        errors={kind: sum(r.error == kind for r in rows) for kind in sorted({r.error for r in rows if r.error})})
    return report


def grouped(rows: Sequence[Observation], threshold: float | None = None) -> dict[str, Any]:
    return {group: summarize(rows if group == "overall" else [r for r in rows if r.case.question.category == group],
                              threshold=threshold) for group in ("overall", "fact", "finance")}


def recommend(baseline: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    failures = []
    for group in ("overall", "finance"):
        left, right = baseline[group], decision[group]
        for count in ("true_hits", "true_misses"):
            if not left[count] or left[count] != right[count]:
                failures.append(f"{group}: {count} are empty or unpaired")
        for metric in ("miss_recall", "false_alarm_rate"):
            delta = abs(right[metric] - left[metric])
            if delta > config.DECISION_METRIC_TOLERANCE:
                failures.append(f"{group}: {metric} differs by {delta:.4f}")
    overall = decision["overall"]
    median = overall["latency_p50_ms"]
    if median is None or median >= config.DECISION_TARGET_LATENCY_MS:
        failures.append(f"median latency {median} ms must be below {config.DECISION_TARGET_LATENCY_MS:g} ms")
    if overall["failed"] or baseline["overall"]["failed"]:
        failures.append("failed calls or fallback responses prevent a complete paired comparison")
    return {"switch": not failures, "recommendation": "switch" if not failures else "stay with LLM", "reasons": failures}


async def compare(split: str, *, engines: Sequence[str], today: date | str,
                  matrix: Path, results_path: Path, limit: int | None = None) -> dict[str, Any]:
    """Compare identical filtered learn rows without fetching any missing search results."""
    if split != ALLOWED_SPLIT:
        raise SplitRefused(f"checker_eval runs on the learn split only, not {split!r}. "
                           "Reported numbers come from the test split, so it is never tuned on.")
    if config.SO_CACHE != "readwrite":
        raise ValueError("Decisions comparison requires SO_CACHE=readwrite")
    if not contamination.enabled():
        raise ValueError("Decisions comparison requires the benchmark contamination filter")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    unknown = set(engines) - set(config.ENGINES)
    if unknown:
        raise ValueError(f"Unknown engines: {sorted(unknown)}")
    selected = [e for e in dict.fromkeys(engines) if e not in config.DECISION_EVAL_SKIP_ENGINES]
    questions = learn_questions()
    qids = {q.id for q in questions}
    truth = load_truth(matrix, qids)
    results = load_results(results_path, qids)
    missing_truth = sum((q.id, engine) not in truth for q in questions for engine in selected)
    if missing_truth:
        raise ValueError(f"{missing_truth} learn searches lack matrix truth; comparison refuses partial inputs")
    # build_cases calls evals.contamination.filter_results, exactly as checker_eval does.
    cases, missing = build_cases(questions, truth, results, selected)
    if missing:
        raise ValueError(f"{len(missing)} learn matrix rows lack stored results; comparison refuses partial inputs")
    if limit is not None:
        cases = cases[:limit]
    if not cases:
        raise ValueError("No eligible learn rows available for comparison")
    config.LLM_BUDGET.limit = min(config.LLM_BUDGET.limit, config.DECISION_EVAL_BUDGET_USD)
    start_spend = config.LLM_BUDGET.spent

    async def observe(case: Case, decision: bool) -> Observation:
        original = results[(case.question.id, case.engine)]
        positions = {id(result): i for i, result in enumerate(original, 1)}
        original_positions = tuple(positions[id(result)] for result in case.results)
        try:
            verdict = await (check if decision else checker.check)(
                case.question.question, case.question.question, case.results, today)
            error = getattr(verdict, "failure", None)
            if verdict.why == checker.MALFORMED_WHY:
                error = "malformed"
            elif verdict.why == checker.TRUNCATED_WHY:
                error = "truncated"
            return Observation(case, truth[(case.question.id, case.engine)], verdict, _usage(verdict), original_positions, error)
        except llm.DecisionCallError as exc:
            return Observation(case, truth[(case.question.id, case.engine)], None, exc.usage, original_positions, exc.kind)
        except checker.CheckerError:
            return Observation(case, truth[(case.question.id, case.engine)], None, LLMResponse(), original_positions, "transport")

    baseline_rows = await asyncio.gather(*(observe(case, False) for case in cases))
    decision_rows = await asyncio.gather(*(observe(case, True) for case in cases))
    baseline, decision = grouped(baseline_rows), grouped(decision_rows)
    thresholds = {str(t): grouped(decision_rows, t) for t in config.DECISION_EVAL_THRESHOLDS}
    recommendation = recommend(baseline, decision)
    improved = []
    for threshold, groups in thresholds.items():
        if recommend(baseline, groups)["switch"] and all(
            groups[group]["miss_recall"] >= decision[group]["miss_recall"]
            and groups[group]["false_alarm_rate"] <= decision[group]["false_alarm_rate"]
            for group in ("overall", "finance")) and any(
                groups[group]["miss_recall"] > decision[group]["miss_recall"]
                or groups[group]["false_alarm_rate"] < decision[group]["false_alarm_rate"]
                for group in ("overall", "finance")):
            improved.append(threshold)
    recommendation["helpful_thresholds"] = improved
    return {"split": split, "engines": selected, "skipped_engines": list(config.DECISION_EVAL_SKIP_ENGINES),
            "rows": len(cases), "today": str(today), "llm": baseline, "decision": decision,
            "thresholds": thresholds, "recommendation": recommendation,
            "live_spend_usd": round(config.LLM_BUDGET.spent - start_spend, 8),
            "spend_cap_usd": config.LLM_BUDGET.limit}


def report(comparison: dict[str, Any]) -> str:
    lines = [f"Paired checker comparison: {comparison['rows']} learn rows; engines: {', '.join(comparison['engines'])}",
             "backend category n miss/hit recall false_alarm rank_agreement failed/unparseable/fallback p50_ms p90_ms USD/check cached"]
    for backend in ("llm", "decision"):
        for category, m in comparison[backend].items():
            lines.append(f"{backend} {category} {m['attempted']} {m['true_misses']}/{m['true_hits']} "
                f"{m['miss_recall']:.4f} {m['false_alarm_rate']:.4f} "
                f"{m['evidence_rank_agreed']}/{m['evidence_rank_count']} "
                f"{m['failed']}/{m['unparseable']}/{m['fallbacks']} "
                f"{m['latency_p50_ms']} {m['latency_p90_ms']} {m['cost_per_check']} {m['cached_checks']}")
    lines += ["", "Decision HIT confidence thresholds: threshold category miss/hit recall false_alarm"]
    for threshold, groups in comparison["thresholds"].items():
        for category, m in groups.items():
            lines.append(f"{threshold} {category} {m['true_misses']}/{m['true_hits']} "
                         f"{m['miss_recall']:.4f} {m['false_alarm_rate']:.4f}")
    lines += ["", "Latency uses original call timings, including cached calls.",
              f"Live spend: ${comparison['live_spend_usd']:.6f}; cap ${comparison['spend_cap_usd']:.2f}.",
              f"Recommendation: {comparison['recommendation']['recommendation']}."]
    lines += comparison["recommendation"]["reasons"]
    lines.append(f"Clearly helpful thresholds: {comparison['recommendation']['helpful_thresholds'] or 'none'}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--compare", action="store_true", required=True)
    parser.add_argument("--split", required=True)
    parser.add_argument("--engines", nargs="+", default=config.DECISION_EVAL_ENGINES)
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--matrix", type=Path, default=config.DECISION_EVAL_MATRIX_PATH)
    parser.add_argument("--results", type=Path, default=config.DECISION_EVAL_RESULTS_PATH)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if args.split != ALLOWED_SPLIT:
        parser.exit(2, f"refused: checker_eval runs on the learn split only, not {args.split!r}. "
                       "Reported numbers come from the test split, so it is never tuned on.\n")
    try:
        result = asyncio.run(compare(args.split, engines=args.engines, today=args.today,
                                    matrix=args.matrix, results_path=args.results, limit=args.limit))
    except (ValueError, OSError, config.BudgetExceeded) as exc:
        parser.exit(1, f"Comparison failed: {exc}\n")
    print(report(result))


if __name__ == "__main__":
    main()
