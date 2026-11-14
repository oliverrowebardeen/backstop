"""Split-conformal bound on wrong HITs from the checker's confidence (lane A4).

    python -m analysis.conformal [--log-dir DIR] [--today YYYY-MM-DD]

A wrong HIT is a checker HIT on a result list the hit matrix says does not
contain the answer. We trust a HIT only when its confidence is at least a
threshold lambda, and pick lambda on the learn split so that

    (wrong HITs with confidence >= lambda + 1) / (HITs with confidence >= lambda + 1) <= alpha

Calibration (learn split only): so.checker_eval's own scoring path on the
first engine (its question loader, matrix join, contamination filter and
checker call; responses come from the llm cache when present).

Lambda is the smallest candidate (a distinct HIT confidence) that satisfies the
bound, exactly as the rule above states. The ratio need not fall monotonically
with lambda: few HITs sit at the very top, so the +1 terms can push the ratio
above alpha there even with zero wrong HITs. A fixed-sequence scan from the top
would therefore stop at once on a learn split this size; the report prints the
ratio at every candidate so the shape is visible. If no candidate satisfies the
bound, it is vacuous at that alpha.

Test (held out): the second_opinion eval logs. For each search on the first
engine alone, the check made on its results, with truth from an offline
judgment of the exact result list it saw (evals.retrieval). Calibration and
test question ids must be disjoint; LeakageError is raised otherwise.

Writes out/conformal.json and out/conformal.md. Analysis only: nothing here is
imported by the agent, the checker or the eval.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from analysis.stats import clopper_pearson
from so.config import FIRST_ENGINE, LOG_DIR, OUT_DIR

logger = logging.getLogger(__name__)

ALPHAS = (0.10, 0.05, 0.15)
HEADLINE_ALPHA = 0.10
DRYRUN_LOG_DIR = OUT_DIR / "logs_dryrun2"
OUT_JSON = OUT_DIR / "conformal.json"
OUT_MD = OUT_DIR / "conformal.md"
# The date shown to the checker during calibration. Fixed so reruns hit the llm cache.
CALIBRATION_DATE = "2026-11-14"


class LeakageError(RuntimeError):
    """Raised when a test-split question id reaches calibration."""


@dataclass(frozen=True)
class CheckPoint:
    """One checker verdict on one result list, with the matrix truth for that list."""

    question_id: str
    verdict: str
    confidence: float
    truth_hit: bool

    @property
    def is_hit(self) -> bool:
        return self.verdict == "HIT"

    @property
    def is_wrong_hit(self) -> bool:
        return self.verdict == "HIT" and not self.truth_hit


@dataclass(frozen=True)
class Threshold:
    """The calibrated threshold for one alpha. lam is None when the bound is vacuous."""

    alpha: float
    lam: float | None
    n_hits: int
    n_wrong: int
    bound: float | None
    vacuous: bool
    n_calibration: int


# ---------------------------------------------------------------- selection

def conformal_ratio(points: Sequence[CheckPoint], lam: float) -> tuple[float, int, int]:
    """((wrong + 1) / (hits + 1), hits, wrong) over HITs with confidence >= lam."""
    kept = [p for p in points if p.is_hit and p.confidence >= lam]
    wrong = sum(p.is_wrong_hit for p in kept)
    return (wrong + 1) / (len(kept) + 1), len(kept), wrong


def select_threshold(points: Sequence[CheckPoint], alpha: float) -> Threshold:
    """Smallest HIT confidence lambda with (wrong>=lambda + 1) / (hits>=lambda + 1) <= alpha."""
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    for lam in sorted({p.confidence for p in points if p.is_hit}):
        ratio, hits, wrong = conformal_ratio(points, lam)
        if ratio <= alpha:
            return Threshold(alpha, lam, hits, wrong, round(ratio, 6), False, len(points))
    return Threshold(alpha, None, 0, 0, None, True, len(points))


def ratio_curve(points: Sequence[CheckPoint]) -> list[dict[str, Any]]:
    """The conformal ratio at every candidate lambda, lowest first."""
    out = []
    for lam in sorted({p.confidence for p in points if p.is_hit}):
        ratio, hits, wrong = conformal_ratio(points, lam)
        out.append({"lambda": lam, "hits": hits, "wrong": wrong, "ratio": round(ratio, 4)})
    return out


def assert_disjoint(calibration_ids: Iterable[str], test_ids: Iterable[str]) -> None:
    """Raise LeakageError if any test id appears among the calibration ids."""
    leaked = sorted(set(calibration_ids) & set(test_ids))
    if leaked:
        raise LeakageError(f"{len(leaked)} test question ids reached calibration, e.g. {leaked[:5]}")


def evaluate(points: Sequence[CheckPoint], lam: float | None) -> dict[str, Any]:
    """Held-out numbers at a frozen lambda: kept HITs, wrong-HIT rate with a 95% CI, coverage."""
    hits = [p for p in points if p.is_hit]
    kept = [] if lam is None else [p for p in hits if p.confidence >= lam]
    wrong = sum(p.is_wrong_hit for p in kept)
    lo, hi = clopper_pearson(wrong, len(kept))
    return {
        "lambda": lam,
        "n_checks": len(points),
        "n_questions": len({p.question_id for p in points}),
        "hits_total": len(hits),
        "hits_kept": len(kept),
        "wrong_hits_kept": wrong,
        "wrong_hit_rate": round(wrong / len(kept), 4) if kept else None,
        "wrong_hit_rate_ci95": [round(lo, 4), round(hi, 4)],
        "coverage": round(len(kept) / len(points), 4) if points else None,
        "hit_retention": round(len(kept) / len(hits), 4) if hits else None,
        "wrong_hits_all": sum(p.is_wrong_hit for p in hits),
    }


# ---------------------------------------------------------------- calibration

async def calibration_points(today: date | str, engine: str = FIRST_ENGINE,
                             forbidden_ids: Iterable[str] = ()) -> list[CheckPoint]:
    """Score the learn split with so.checker_eval's own path and pair each verdict with matrix truth.

    Raises LeakageError if any id in `forbidden_ids` (the test split) would be calibrated on.
    """
    from so import checker_eval as ce

    questions = ce.learn_questions()  # raises SplitRefused on any non-learn question
    assert_disjoint((q.id for q in questions), forbidden_ids)
    qids = {q.id for q in questions}
    truth = ce.load_truth(ce.MATRIX, qids)
    results = ce.load_results(ce.RESULTS, qids)
    cases, missing = ce.build_cases(questions, truth, results, [engine])
    if missing:
        logger.warning("%d learn rows had no stored results and were skipped", len(missing))
    scored = await ce.score(cases, today)
    usage = [s.verdict for s in scored]
    live = sum(not getattr(v, "cached", True) for v in usage)
    logger.info("calibration: %d checks, %d live, $%.4f live spend", len(usage), live,
                sum(getattr(v, "cost_usd", 0.0) for v in usage if not getattr(v, "cached", True)))
    return [CheckPoint(s.case.question.id, s.verdict.verdict, float(s.verdict.confidence), s.case.truth_hit)
            for s in scored]


# ---------------------------------------------------------------- test

async def test_points(log_dir: Path, test_ids: set[str],
                      engine: str = FIRST_ENGINE) -> tuple[list[CheckPoint], dict[str, int]]:
    """First-engine checks from finished second_opinion logs of test questions.

    Truth is an offline judgment of the exact result list each check saw
    (evals.retrieval), not a matrix row for a different, question-text search.
    """
    from data.load import load_all
    from evals.retrieval import judge_runs
    from evals.run_eval import finished_runs
    from evals.score import OURS

    finished = {k: p for k, p in finished_runs(log_dir).items() if k[1] == OURS}
    runs = {k: p for k, p in finished.items() if k[0] in test_ids}
    truth = await judge_runs(runs, {q.id: q for q in load_all()})
    points = [CheckPoint(s.question_id, str(s.verdict), float(s.confidence or 0.0), bool(s.hit))
              for s in truth.searches
              if s.engines == [engine] and s.verdict is not None and s.hit is not None]
    counts = {"finished_runs": len(runs), "outside_test": len(finished) - len(runs),
              "not_in_cache": truth.missing, "judge_errors": truth.judge_errors}
    return points, counts


def resolve_log_dir(log_dir: Path | None) -> tuple[Path, str]:
    """The log directory and a label. Falls back to the dry run until the full eval has logs."""
    from evals.run_eval import finished_runs

    if log_dir is not None:
        return log_dir, "logs" if Path(log_dir).name == "logs" else f"logs from {_display_path(log_dir)}"
    if finished_runs(LOG_DIR):
        return LOG_DIR, "logs"
    if DRYRUN_LOG_DIR.exists():
        return DRYRUN_LOG_DIR, "DRY RUN logs (full eval logs not available yet)"
    raise FileNotFoundError(f"no finished logs in {LOG_DIR} or {DRYRUN_LOG_DIR}")


# ---------------------------------------------------------------- report

def _display_path(path: Path) -> str:
    """Path relative to the repo root when inside it, else just its last two parts (no home paths)."""
    from so.config import ROOT

    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(Path(*Path(path).parts[-2:]))


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def slide_sentence(head: Threshold, test: dict[str, Any]) -> str:
    """One plain sentence for the headline alpha."""
    a = f"{100 * head.alpha:.0f}%"
    if head.vacuous:
        return (f"At a {a} wrong-HIT target the learn split is too small to certify any confidence "
                f"threshold, so the bound is vacuous and we make no conformal claim.")
    lo, hi = test["wrong_hit_rate_ci95"]
    if not test["hits_kept"]:
        return (f"Calibrated on the learn split for at most {a} wrong HITs (threshold {head.lam:.2f}); "
                f"no held-out HITs cleared it yet, so there is no test number.")
    return (f"Calibrated on the learn split to keep wrong HITs at or below {a}, checker HITs at confidence "
            f"{head.lam:.2f} or higher were wrong {_pct(test['wrong_hit_rate'])} of the time on "
            f"{test['hits_kept']} held-out searches (95% CI {_pct(lo)} to {_pct(hi)}), "
            f"covering {_pct(test['coverage'])} of first-engine searches"
            + (f"; that is above the {a} target." if test["wrong_hit_rate"] > head.alpha else "."))


def render_md(doc: dict[str, Any]) -> str:
    """Short markdown report."""
    L = ["# Conformal bound on wrong HITs", ""]
    if "DRY RUN" in doc["test_source"]:
        L += ["**Test numbers below come from the 20-question DRY RUN, not the full eval. Rerun after "
              "the eval finishes.**", ""]
    L += [f"Slide line: {doc['slide_sentence']}", "",
          f"Leakage check: {doc['leakage']['learn_ids']} learn question ids, {doc['leakage']['test_ids']} "
          f"test question ids, overlap {doc['leakage']['overlap']}. Test logs: {doc['test_source']} "
          f"({doc['test_counts']['finished_runs']} finished second_opinion runs).", "",
          "| alpha | lambda | learn HITs kept | learn wrong | learn ratio | test HITs kept | test wrong "
          "| test wrong-HIT rate (95% CI) | coverage | HIT retention |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for row in doc["alphas"]:
        c, t = row["calibration"], row["test"]
        lam = "vacuous" if c["vacuous"] else f"{c['lam']:.2f}"
        lo, hi = t["wrong_hit_rate_ci95"]
        bound = "n/a" if c["bound"] is None else f"{c['bound']:.3f}"
        L.append(f"| {c['alpha']:.2f} | {lam} | {c['n_hits']} | {c['n_wrong']} | "
                 f"{bound} | {t['hits_kept']} | "
                 f"{t['wrong_hits_kept']} | {_pct(t['wrong_hit_rate'])} ({_pct(lo)} to {_pct(hi)}) | "
                 f"{_pct(t['coverage'])} | {_pct(t['hit_retention'])} |")
    vac = [r["calibration"]["alpha"] for r in doc["alphas"] if r["calibration"]["vacuous"]]
    if vac:
        L += ["", "The bound is vacuous at alpha " + ", ".join(f"{a:.2f}" for a in vac)
              + ": no threshold certifies it on the learn split."]
    cal = doc["calibration_summary"]
    L += ["", "Learn-split ratio (wrong + 1) / (HITs + 1) at each candidate lambda: "
          + ", ".join(f"{r['lambda']:.2f}: {r['ratio']:.3f} ({r['wrong']}/{r['hits']})"
                      for r in doc["calibration_curve"]) + ".",
          "",
          f"Calibration: {cal['n_checks']} learn searches on {doc['engine']}, {cal['hits']} HITs, "
          f"{cal['wrong_hits']} wrong. The checker already turns HITs below its own minimum confidence into "
          "SHALLOW, so every HIT here clears that floor.",
          "",
          "Test truth: an offline judgment of the exact (contamination-filtered) result list each "
          "first-engine check saw, recovered from the Telem response cache (evals/retrieval.py). "
          "Caveats: calibration checks use the question as query and goal while test checks use the agent's, so "
          "exchangeability is approximate. Coverage is kept HITs over all first-engine checks; HIT retention "
          "is kept HITs over all HITs.", ""]
    return "\n".join(L)


async def run(log_dir: Path | None = None, today: date | str | None = None,
              alphas: Sequence[float] = ALPHAS, engine: str = FIRST_ENGINE) -> dict[str, Any]:
    """Calibrate on learn, evaluate on test logs, and return the report document."""
    from data.load import load_questions

    today = today or CALIBRATION_DATE
    learn_ids = {q.id for q in load_questions(split="learn")}
    test_ids = {q.id for q in load_questions(split="test") if not q.is_trap}
    assert_disjoint(learn_ids, test_ids)
    cal = await calibration_points(today, engine, forbidden_ids=test_ids)
    assert_disjoint((p.question_id for p in cal), test_ids)

    log_path, source = resolve_log_dir(log_dir)
    tests, counts = await test_points(log_path, test_ids, engine)
    assert_disjoint((p.question_id for p in cal), (p.question_id for p in tests))

    rows = []
    for alpha in alphas:
        th = select_threshold(cal, alpha)
        rows.append({"calibration": asdict(th), "test": evaluate(tests, th.lam)})
    head_row = next(r for r in rows if r["calibration"]["alpha"] == HEADLINE_ALPHA) if HEADLINE_ALPHA in alphas else rows[0]
    head = Threshold(**head_row["calibration"])
    doc = {
        "engine": engine,
        "headline_alpha": head.alpha,
        "slide_sentence": slide_sentence(head, head_row["test"]),
        "test_source": source,
        "test_log_dir": _display_path(log_path),
        "test_counts": counts,
        "leakage": {"learn_ids": len({p.question_id for p in cal}), "test_ids": len({p.question_id for p in tests}),
                    "overlap": len({p.question_id for p in cal} & {p.question_id for p in tests})},
        "calibration_summary": {"split": "learn", "today": str(today), "n_checks": len(cal),
                                "hits": sum(p.is_hit for p in cal), "wrong_hits": sum(p.is_wrong_hit for p in cal)},
        "test_summary": evaluate(tests, None) | {"hits_total": sum(p.is_hit for p in tests),
                                                  "wrong_hits_all": sum(p.is_wrong_hit for p in tests)},
        "alphas": rows,
        "calibration_curve": ratio_curve(cal),
        "rule": "lambda = smallest HIT confidence such that (wrong>=lambda + 1)/(hits>=lambda + 1) <= alpha",
    }
    return doc


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point: writes out/conformal.json and out/conformal.md."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--log-dir", type=Path, help=f"eval logs (default {LOG_DIR}, else the dry run)")
    p.add_argument("--today", default=None, help=f"date shown to the checker (default {CALIBRATION_DATE})")
    p.add_argument("--out-json", type=Path, default=OUT_JSON)
    p.add_argument("--out-md", type=Path, default=OUT_MD)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    doc = asyncio.run(run(a.log_dir, a.today))
    a.out_json.parent.mkdir(parents=True, exist_ok=True)
    a.out_json.write_text(json.dumps(doc, indent=2) + "\n")
    md = render_md(doc)
    a.out_md.write_text(md)
    logger.info("%s\nwrote %s and %s", md, a.out_json, a.out_md)


if __name__ == "__main__":
    main()
