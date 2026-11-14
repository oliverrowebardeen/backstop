"""Optional Decisions checker and paired learn-only evaluation.

Run ``python -m so.decision_checker --compare --split learn``. The default
Tavily rows match checker_eval; DECISION_EVAL_ENGINES selects other engines.
Search result lists use ``configured_check`` to select CHECKER_BACKEND;
page checks keep the LLM checker. Call ``check`` to explicitly use Decisions.

All settings are environment-backed in so.config. Comparisons share its
$3 process budget, require disk caching, and exclude ceramic by default.
Latency uses original recorded call timings, including cached calls.

``--cascade-eval --split learn`` collects the five configured engines under
a $2 cap, splits by question ID with seed 14, and freezes the selected
threshold before scoring the heldout half. Reports stay in the cache directory.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any

from pydantic import Field

from so import checker, config, llm
from so.checker_eval import (
    ALLOWED_SPLIT, Case, Scored, SplitRefused, build_cases, contamination, learn_questions,
    load_results, load_truth, metrics,
)
from so.contracts import CheckerVerdict, LLMResponse, MatrixRow, Result, RunContext

logger = logging.getLogger(__name__)
_fallback_warning_lock = Lock()
_fallback_warning_emitted = False


def _warn_fallback_once(reason: str) -> None:
    """Report degraded checker availability once per process, including timeouts."""
    global _fallback_warning_emitted
    with _fallback_warning_lock:
        if _fallback_warning_emitted:
            return
        _fallback_warning_emitted = True
    logger.warning("Decisions unavailable (%s); falling back to the configured LLM checker. "
                   "Further fallback warnings are suppressed for this process.", reason)


class DecisionVerdict(checker.CheckedVerdict):
    """Contract verdict with explicit fallback and usage metadata."""

    failure: str | None = Field(default=None, exclude=True)
    fallback: bool = Field(default=False, exclude=True)
    decision_calls: int = Field(default=0, ge=0, exclude=True)


class CascadeVerdict(DecisionVerdict):
    """Final verdict with separate escalation and recovered decision-failure metadata."""

    escalated: bool = Field(default=False, exclude=True)
    decision_failure: str | None = Field(default=None, exclude=True)


class _DecisionBudgetExceeded(config.BudgetExceeded):
    """Retain completed Decisions usage when its separate budget runs out."""

    def __init__(self, error: config.BudgetExceeded, usage: LLMResponse) -> None:
        super().__init__(str(error), budget=error.budget)
        self.usage = usage


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
        except config.BudgetExceeded as exc:
            if exc.budget is config.DECISION_BUDGET:
                raise _DecisionBudgetExceeded(exc, total) from exc
            raise
        except (llm.DecisionCallError, llm.DecisionStorageError) as exc:
            total = _sum_usage(total, exc.usage)
            checker._log_usage(ctx, exc.usage)
            raise
        total = _sum_usage(total, response)
        checker._log_usage(ctx, response)
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
        if exc.kind != "timeout":
            raise llm.DecisionCallError(str(exc), usage=total, kind=exc.kind) from exc
        _warn_fallback_once("timeout")
        try:
            fallback = await checker.check(query, goal, top, today, ctx=ctx)
        except config.BudgetExceeded:
            raise
        except checker.CheckerError as fallback_error:
            raise llm.DecisionCallError("Decisions timed out and the LLM fallback failed",
                usage=_sum_usage(total, fallback_error.usage), kind="fallback_failed") from fallback_error
        return _with_usage(fallback, _sum_usage(total, _usage(fallback)),
                           failure="timeout", fallback=True, decision_calls=calls)
    except llm.DecisionStorageError as exc:
        raise llm.DecisionStorageError(str(exc), usage=total) from exc
    return _with_usage(verdict, total, decision_calls=calls)


async def configured_check(query: str, goal: str, results: Sequence[Result | dict[str, Any]],
                           today: date | str | None = None, *, ctx: RunContext | None = None) -> checker.CheckedVerdict:
    """Dispatch result lists without changing the LLM checker's input or output."""
    if config.CHECKER_BACKEND == "cascade":
        return await cascade_check(query, goal, results, today, ctx=ctx)
    if config.CHECKER_BACKEND == "decision":
        return await check(query, goal, results, today, ctx=ctx)
    from so.checker import check as llm_check

    # Preserve the search loop's support for checker adapters without context.
    kwargs: dict[str, Any] = {}
    try:
        params = inspect.signature(llm_check).parameters
    except (TypeError, ValueError):
        params = {}
    if "ctx" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kwargs["ctx"] = ctx
    return await llm_check(query, goal, results, today, **kwargs)


def _needs_escalation(verdict: CheckerVerdict, threshold: float) -> bool:
    return verdict.verdict == "HIT" and verdict.confidence < threshold


def _cascade_result(decision: DecisionVerdict | None, final: checker.CheckedVerdict,
                    usage: LLMResponse, *, escalated: bool, failure: str | None = None,
                    decision_calls: int | None = None) -> CascadeVerdict:
    """Keep Decisions pointers on success, otherwise the LLM fallback's pointers."""
    fields = final.model_dump()
    evidence = decision if decision is not None else final
    fields.update(evidence_rank=evidence.evidence_rank, best_url=evidence.best_url)
    return CascadeVerdict(**fields, **{name: getattr(usage, name) for name in (
        "model", "tokens_in", "tokens_out", "cost_usd", "latency_ms", "cached")},
        escalated=escalated, fallback=failure is not None, decision_failure=failure,
        decision_calls=decision_calls if decision_calls is not None else (decision.decision_calls if decision else 1))


async def cascade_check(query: str, goal: str, results: Sequence[Result | dict[str, Any]],
                        today: date | str | None = None, *, ctx: RunContext | None = None) -> CascadeVerdict:
    """Escalate uncertain HITs and operational failures to the LLM checker.

    Decisions non-HITs are final. At the threshold, a HIT is accepted. An
    escalated response keeps Decisions' evidence pointers even when the LLM
    picks another result. If Decisions failed, keep the LLM's evidence pointers.
    Decisions budget exhaustion falls back; LLM exhaustion and cancellation propagate.
    """
    top = checker.as_results(results)[:config.DECISION_TOP_K]
    decision = None
    failure = None
    usage = LLMResponse(role="checker", cached=True)
    try:
        if top:
            try:
                config.api_key("openai")
            except ValueError:
                failure = "missing_api_key"
        if failure is None:
            decision = await check(query, goal, top, today, ctx=ctx)
    except _DecisionBudgetExceeded as exc:
        failure, usage = "decision_budget_exhausted", exc.usage
    except llm.DecisionCallError as exc:
        if exc.kind == "fallback_failed":
            raise checker.CheckerError(str(exc), kind=exc.kind, usage=exc.usage) from exc
        failure, usage = exc.kind, exc.usage
    except (ValueError, OSError) as exc:
        # Invalid provider configuration or unreadable cache must not become a verdict.
        failure = type(exc).__name__
        usage = LLMResponse(role="checker")
        if isinstance(exc, llm.DecisionStorageError):
            usage = exc.usage
    if decision is not None:
        usage = _usage(decision)
        if decision.fallback:
            # check() already made the timeout fallback; do not call the LLM twice.
            return _cascade_result(None, decision, usage, escalated=True, failure=decision.failure)
        if not _needs_escalation(decision, config.CHECKER_CASCADE_THRESHOLD):
            return _cascade_result(decision, decision, usage, escalated=False)
    if failure:
        _warn_fallback_once(failure)
    try:
        final = await checker.check(query, goal, top, today, ctx=ctx)
    except checker.CheckerError as exc:
        raise checker.CheckerError("Cascade LLM escalation failed", kind=exc.kind,
                                   usage=_sum_usage(usage, exc.usage)) from exc
    return _cascade_result(decision, final, _sum_usage(usage, _usage(final)),
                           escalated=True, failure=failure,
                           decision_calls=0 if failure == "missing_api_key" else None)


@dataclass
class Observation:
    case: Case
    truth: MatrixRow
    verdict: checker.CheckedVerdict | None
    usage: LLMResponse
    original_positions: tuple[int, ...]
    error: str | None = None


@dataclass(frozen=True)
class CheckerPair:
    """Both checker outputs for one identical, filtered learn search."""

    baseline: Observation
    decision: Observation


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


def simulate_cascade(pair: CheckerPair, threshold: float) -> Observation:
    """Replay the cascade from independent outputs with sequential call costs."""
    if pair.baseline.error or pair.decision.error:
        raise ValueError("Cascade calibration requires complete independent checker responses")
    decision, baseline = pair.decision.verdict, pair.baseline.verdict
    if not isinstance(decision, DecisionVerdict) or baseline is None or decision.fallback:
        raise ValueError("Cascade calibration requires a successful Decisions response")
    escalated = _needs_escalation(decision, threshold)
    final = baseline if escalated else decision
    usage = _sum_usage(pair.decision.usage, pair.baseline.usage) if escalated else pair.decision.usage
    verdict = _cascade_result(decision, final, usage, escalated=escalated)
    row = pair.decision
    return Observation(row.case, row.truth, verdict, usage, row.original_positions)


def cascade_metrics(rows: Sequence[Observation]) -> dict[str, Any]:
    """Checker metrics plus exact selection counts and sequential-check latency."""
    result = summarize(rows)
    n = len(rows)
    latencies = sorted(row.usage.latency_ms for row in rows)
    escalated = sum(bool(getattr(row.verdict, "escalated", False)) for row in rows)
    result.update(n_questions=len({row.case.question.id for row in rows}),
        no_model_calls=sum(not row.usage.model for row in rows),
        misses_flagged=result["true_misses"] - result["confusion"]["miss"]["HIT"],
        hits_flagged=result["true_hits"] - result["confusion"]["hit"]["HIT"],
        escalated=escalated, share_escalated=round(escalated / n, 6) if n else None,
        latency_mean_ms=round(statistics.mean(latencies), 2) if n else None,
        latency_p90_ms=round(latencies[math.ceil(n * .9) - 1], 2) if n else None,
        latency_count=n)
    return result


def split_pairs(pairs: Sequence[CheckerPair], seed: int) -> tuple[list[CheckerPair], list[CheckerPair]]:
    """Shuffle sorted question IDs, keeping every engine for a question together."""
    if any(p.baseline.case.question.split != ALLOWED_SPLIT or p.baseline.case.question.is_trap for p in pairs):
        raise SplitRefused("Cascade calibration and holdout must both come from learn questions")
    ids = sorted({p.baseline.case.question.id for p in pairs})
    if len(ids) < 2:
        raise ValueError("Cascade evaluation needs at least two learn questions")
    random.Random(seed).shuffle(ids)
    calibration_ids = set(ids[:len(ids) // 2])
    return ([p for p in pairs if p.baseline.case.question.id in calibration_ids],
            [p for p in pairs if p.baseline.case.question.id not in calibration_ids])


def select_threshold(calibration: Sequence[CheckerPair], thresholds: Sequence[float]) -> dict[str, Any]:
    """Maximize recall under the LLM false-alarm count, using calibration only.

    Counts avoid selection artifacts from rounded rates. Ties favor fewer
    escalations, then the lower threshold. No feasible candidate means no switch.
    """
    baseline = cascade_metrics([p.baseline for p in calibration])
    if not baseline["true_misses"] or not baseline["true_hits"]:
        raise ValueError("Calibration needs both true misses and true hits")
    candidates = {}
    for threshold in thresholds:
        if not 0 <= threshold <= 1:
            raise ValueError("Cascade thresholds must be between 0 and 1")
        candidates[threshold] = cascade_metrics([simulate_cascade(p, threshold) for p in calibration])
    eligible = [t for t, result in candidates.items() if result["hits_flagged"] <= baseline["hits_flagged"]]
    selected = max(eligible, key=lambda t: (candidates[t]["misses_flagged"],
                                          -candidates[t]["escalated"], -t)) if eligible else None
    return {"threshold": selected, "baseline": baseline, "candidates": candidates,
            "question_ids": sorted({p.baseline.case.question.id for p in calibration}),
            "selection_rule": "maximize miss recall with false alarms no higher than LLM; ties: fewer escalations, then lower threshold"}


def cascade_groups(pairs: Sequence[CheckerPair], threshold: float | None) -> dict[str, Any]:
    """Report overall, category and engine results for exactly the same rows."""
    groups = {"overall": list(pairs)}
    for category in sorted({p.baseline.case.question.category for p in pairs}):
        groups[f"category/{category}"] = [p for p in pairs if p.baseline.case.question.category == category]
    for engine in sorted({p.baseline.case.engine for p in pairs}):
        groups[f"engine/{engine}"] = [p for p in pairs if p.baseline.case.engine == engine]
    result = {}
    for group, subset in groups.items():
        result[group] = {"llm": cascade_metrics([p.baseline for p in subset]),
                         "decision": cascade_metrics([p.decision for p in subset])}
        if threshold is not None:
            result[group]["cascade"] = cascade_metrics([simulate_cascade(p, threshold) for p in subset])
    return result


def recommend_cascade(heldout: dict[str, Any]) -> dict[str, Any]:
    """Apply the phase 2 switching rule only to the frozen threshold's heldout half."""
    overall = heldout["overall"]
    reasons = []
    if "cascade" not in overall:
        return {"switch": False, "recommendation": "stay with LLM", "reasons": ["No feasible calibration threshold"]}
    baseline, cascade = overall["llm"], overall["cascade"]
    if not baseline["true_misses"] or not baseline["true_hits"]:
        reasons.append("Heldout half needs both true misses and true hits")
    if any(baseline[key] != cascade[key] for key in ("attempted", "true_hits", "true_misses")):
        reasons.append("Heldout checker rows are not paired")
    if cascade["misses_flagged"] < baseline["misses_flagged"]:
        reasons.append(f"Heldout miss recall fell: {cascade['misses_flagged']}/{cascade['true_misses']} "
                       f"versus LLM {baseline['misses_flagged']}/{baseline['true_misses']}")
    if cascade["hits_flagged"] > baseline["hits_flagged"]:
        reasons.append(f"Heldout false alarms rose: {cascade['hits_flagged']}/{cascade['true_hits']} "
                       f"versus LLM {baseline['hits_flagged']}/{baseline['true_hits']}")
    if baseline["failed"] or cascade["failed"]:
        reasons.append("Heldout checks have operational failures")
    return {"switch": not reasons, "recommendation": "cascade" if not reasons else "stay with LLM", "reasons": reasons}


async def collect_pairs(split: str, *, engines: Sequence[str], today: date | str,
                        matrix: Path, results_path: Path) -> list[CheckerPair]:
    """Collect independent outputs under the combined $2 cap, never search or judge."""
    if split != ALLOWED_SPLIT:
        raise SplitRefused("Cascade evaluation reads the learn split only, never test or traps")
    if config.SO_CACHE != "readwrite" or not contamination.enabled():
        raise ValueError("Cascade evaluation requires caching and the contamination filter")
    selected = list(dict.fromkeys(engines))
    if not selected or set(selected) - (set(config.ENGINES) - set(config.DECISION_EVAL_SKIP_ENGINES)):
        raise ValueError("Cascade engines must be configured engines excluding skipped engines")
    questions = learn_questions()
    ids = {q.id for q in questions}
    truth = load_truth(matrix, ids)
    results = load_results(results_path, ids)
    cases, missing = build_cases(questions, truth, results, selected)
    if missing or len(cases) != len(questions) * len(selected):
        raise ValueError("Cascade evaluation requires complete stored learn results and matrix truth for every engine")
    config.LLM_BUDGET.limit = min(config.LLM_BUDGET.limit, config.CASCADE_EVAL_BUDGET_USD)
    config.DECISION_BUDGET.limit = min(config.DECISION_BUDGET.limit, config.CASCADE_EVAL_BUDGET_USD)

    async def one(case: Case) -> CheckerPair:
        baseline = await checker.check(case.question.question, case.question.question, case.results, today)
        decision = await check(case.question.question, case.question.question, case.results, today)
        if decision.failure or decision.fallback:
            raise ValueError("Independent Decisions check failed; calibration refuses fallback-contaminated outputs")
        original = {id(r): i for i, r in enumerate(results[(case.question.id, case.engine)], 1)}
        positions = tuple(original[id(r)] for r in case.results)
        row = truth[(case.question.id, case.engine)]
        return CheckerPair(Observation(case, row, baseline, _usage(baseline), positions),
                           Observation(case, row, decision, _usage(decision), positions))

    pairs = []
    concurrency = min(config.LLM_MAX_CONCURRENCY, config.DECISION_MAX_CONCURRENCY)
    for start in range(0, len(cases), concurrency):
        pairs.extend(await asyncio.gather(*(one(c) for c in cases[start:start + concurrency])))
    return pairs


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


async def evaluate_cascade(split: str, *, engines: Sequence[str], today: date | str,
                           matrix: Path, results_path: Path) -> dict[str, Any]:
    """Freeze calibration before computing heldout metrics; never change the runtime backend."""
    spent = config.LLM_BUDGET.spent
    pairs = await collect_pairs(split, engines=engines, today=today, matrix=matrix, results_path=results_path)
    calibration, heldout = split_pairs(pairs, config.CASCADE_EVAL_SEED)
    selection = select_threshold(calibration, config.CASCADE_EVAL_THRESHOLDS)
    selection.update(seed=config.CASCADE_EVAL_SEED, split=ALLOWED_SPLIT, engines=list(engines), today=str(today))
    # Persist the choice before heldout metrics are calculated, including its question partition.
    _write_report(config.CASCADE_CALIBRATION_PATH, selection)
    threshold = selection["threshold"]
    calibration_report = cascade_groups(calibration, threshold)
    heldout_report = cascade_groups(heldout, threshold)
    first_engine = [p for p in pairs if p.baseline.case.engine == config.FIRST_ENGINE]
    sweep = {"llm": cascade_metrics([p.baseline for p in first_engine]),
             "decision": cascade_metrics([p.decision for p in first_engine])}
    for t in config.CASCADE_EVAL_THRESHOLDS:
        sweep[f"cascade_{t:g}"] = cascade_metrics([simulate_cascade(p, t) for p in first_engine])
    result = {"split": ALLOWED_SPLIT, "seed": config.CASCADE_EVAL_SEED, "engines": list(engines),
        "threshold": threshold, "selection": selection, "first_engine_sweep": sweep, "sweep_engine": config.FIRST_ENGINE,
        "calibration": calibration_report, "heldout": heldout_report,
        "all_learn_llm_by_engine": {engine: cascade_metrics([p.baseline for p in pairs
            if p.baseline.case.engine == engine]) for engine in engines},
        "recommendation": recommend_cascade(heldout_report),
        "live_spend_usd": round(config.LLM_BUDGET.spent - spent, 8),
        "spend_cap_usd": config.LLM_BUDGET.limit,
        "caveat": "Heldout means half of learn, not test. Tavily exploratory results used all learn questions before this split."}
    _write_report(config.CASCADE_EVAL_REPORT_PATH, result)
    return result


def cascade_report(result: dict[str, Any]) -> str:
    def line(backend: str, group: str, m: dict[str, Any]) -> str:
        return (f"{backend} {group} {m['n_questions']} {m['attempted']} "
                f"{m['true_misses']}/{m['true_hits']} {m['miss_recall']:.4f} {m['false_alarm_rate']:.4f} "
                f"{m['escalated']}/{m['attempted']} {m['latency_mean_ms']} {m['latency_p90_ms']} "
                f"{m['cost_per_check']} {m['failed']}/{m['unparseable']}")

    header = "backend group questions rows misses/hits recall false_alarm escalated mean_ms p90_ms USD/check failed/unparseable"
    lines = [f"Cached {result['sweep_engine']} cascade sweep", header]
    lines += [line(backend, "overall", m) for backend, m in result["first_engine_sweep"].items()]
    lines += [f"\nQuestion split seed: {result['seed']}; frozen threshold: {result['threshold']}",
              "Calibration candidates", header]
    lines += [line(f"cascade_{float(t):g}", "overall", m) for t, m in result["selection"]["candidates"].items()]
    for half in ("calibration", "heldout"):
        lines += [f"\n{half}", header]
        for group, backends in result[half].items():
            lines += [line(backend, group, m) for backend, m in backends.items()]
    lines += ["\nLLM alone across all learn questions", header]
    lines += [line("llm", engine, m) for engine, m in result["all_learn_llm_by_engine"].items()]
    lines += ["", result["caveat"], "Escalations sum the original latency and cost of both calls in sequence.",
              f"Live spend ${result['live_spend_usd']:.8f}; cap ${result['spend_cap_usd']:.2f}.",
              f"Recommendation: {result['recommendation']['recommendation']}."]
    lines.extend(result["recommendation"]["reasons"])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--compare", action="store_true")
    modes.add_argument("--cascade-eval", action="store_true", help="collect five-engine learn rows and calibrate a question-level cascade")
    parser.add_argument("--split", required=True)
    parser.add_argument("--engines", nargs="+")
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--matrix", type=Path, default=config.DECISION_EVAL_MATRIX_PATH)
    parser.add_argument("--results", type=Path, default=config.DECISION_EVAL_RESULTS_PATH)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if args.split != ALLOWED_SPLIT:
        parser.exit(2, f"refused: checker_eval runs on the learn split only, not {args.split!r}. "
                       "Reported numbers come from the test split, so it is never tuned on.\n")
    if args.cascade_eval:
        if args.limit is not None:
            parser.error("--cascade-eval requires complete question groups; --limit is not supported")
        try:
            result = asyncio.run(evaluate_cascade(args.split, engines=args.engines or config.CASCADE_EVAL_ENGINES,
                today=args.today, matrix=args.matrix, results_path=args.results))
        except (ValueError, OSError, config.BudgetExceeded, checker.CheckerError, llm.DecisionCallError) as exc:
            parser.exit(1, f"Cascade evaluation failed: {exc}\n")
        print(cascade_report(result))
        return
    try:
        result = asyncio.run(compare(args.split, engines=args.engines or config.DECISION_EVAL_ENGINES, today=args.today,
                                    matrix=args.matrix, results_path=args.results, limit=args.limit))
    except (ValueError, OSError, config.BudgetExceeded) as exc:
        parser.exit(1, f"Comparison failed: {exc}\n")
    print(report(result))


if __name__ == "__main__":
    main()
