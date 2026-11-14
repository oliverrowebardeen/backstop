"""Choose a first engine from query text and learn-split retrieval counts.

Run ``python -m so.router build`` to atomically write FIRST_ENGINE_TABLE_PATH
and print learn-only measurements. ``python -m so.router measure`` recomputes
the measurements without writing. Paths, candidate engines, prices and the
finance regex come from so.config. FIRST_ENGINE_ROUTING=off or a missing table
uses FIRST_ENGINE; invalid existing tables raise RoutingTableError.

The matrix simulation assumes a perfect HIT/DEAD checker and engine retries.
It cannot measure page reads, rewritten queries, operational failures, live
checker accuracy, cost or latency. Reported coverage is not answer accuracy.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Any, Literal, get_args
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from so import config
from so.contracts import CheckerVerdict, MatrixRow, Question, RescueTable
from so.policy import load_rescue_table, next_action

Category = Literal["fact", "finance"]


class LearnOnlyError(ValueError):
    """A routing build was given held-out, trap or unidentified evidence."""


class RoutingTableError(ValueError):
    """An existing routing table is invalid or incompatible with configuration."""


class EngineCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    n_questions: int = Field(gt=0)
    hits: int = Field(ge=0)
    hit_rate: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def consistent_counts(self) -> EngineCounts:
        if self.hits > self.n_questions or not math.isclose(self.hit_rate, self.hits / self.n_questions):
            raise ValueError("hit_rate must equal hits / n_questions")
        return self


class FirstEngineTable(BaseModel):
    """Routing choices and the learn-only counts and prices that justify them."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    built_on_split: Literal["learn"]
    n_questions: int = Field(gt=0)
    routing: dict[Category, str]
    counts: dict[Category, dict[str, EngineCounts]]
    price_usd: dict[str, float]

    @model_validator(mode="after")
    def consistent_table(self) -> FirstEngineTable:
        categories = set(get_args(Category))
        if set(self.routing) != categories or set(self.counts) != categories:
            raise ValueError("routing table must contain fact and finance")
        if not self.price_usd or any(not engine.strip() for engine in self.price_usd):
            raise ValueError("routing table needs named engines and prices")
        if any(price < 0 for price in self.price_usd.values()):
            raise ValueError("engine prices must be nonnegative")
        total = 0
        for category, counts in self.counts.items():
            if set(counts) != set(self.price_usd):
                raise ValueError("every category must cover the same priced engines")
            sizes = {item.n_questions for item in counts.values()}
            if len(sizes) != 1:
                raise ValueError("engine hit rates must use the same question cohort")
            total += sizes.pop()
            winner = min(counts, key=lambda engine: (-counts[engine].hits, self.price_usd[engine], engine))
            if self.routing[category] != winner:
                raise ValueError("routing choice must maximize hits, breaking ties by price then engine name")
        if total != self.n_questions:
            raise ValueError("category counts must sum to n_questions")
        return self


def classify(query: str, goal: str) -> Category:
    """Classify text only, without question IDs, labels, files or model calls."""
    text = " ".join(unicodedata.normalize("NFKC", f"{query} {goal}").split())
    return "finance" if re.search(config.FIRST_ENGINE_FINANCE_PATTERN, text, re.IGNORECASE) else "fact"


def first_engine(query: str, goal: str) -> str:
    """Route the first call; disabled routing and absent tables use FIRST_ENGINE."""
    if config.FIRST_ENGINE_ROUTING == "off":
        return config.FIRST_ENGINE
    target = Path(config.FIRST_ENGINE_TABLE_PATH)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return config.FIRST_ENGINE
    except OSError as exc:
        raise RoutingTableError(f"Cannot read first-engine table {target.name}") from exc
    try:
        table = FirstEngineTable.model_validate_json(raw)
    except ValidationError as exc:
        raise RoutingTableError(f"Invalid first-engine table {target.name}; rebuild with python -m so.router build") from exc
    engine = table.routing[classify(query, goal)]
    if engine not in config.ENGINES:
        raise RoutingTableError(f"Routed engine {engine!r} is not in ENGINES; rebuild the routing table")
    return engine


def _learn_index(questions: Sequence[Question]) -> dict[str, Question]:
    if not questions:
        raise ValueError("No learn questions available")
    info: dict[str, Question] = {}
    for question in questions:
        if question.split != "learn" or question.is_trap or question.category == "trap":
            raise LearnOnlyError("Refusing test or trap questions in a routing build")
        if question.id in info:
            raise ValueError("Duplicate learn question ID")
        info[question.id] = question
    return info


def _learn_hits(
    rows: Iterable[MatrixRow | Mapping[str, Any]], questions: Sequence[Question],
) -> dict[str, dict[str, bool]]:
    info = _learn_index(questions)
    hits: dict[str, dict[str, bool]] = {key: {} for key in info}
    for raw in rows:
        if isinstance(raw, Mapping):
            if raw.get("split", "learn") != "learn" or raw.get("is_trap", False) or raw.get("category") == "trap":
                raise LearnOnlyError("Refusing test or trap matrix rows in a routing build")
            row = MatrixRow.model_validate(dict(raw), strict=True)
        else:
            row = raw
        if row.question_id not in info:
            raise LearnOnlyError("Refusing matrix rows outside the supplied learn questions")
        question_hits = hits[row.question_id]
        if row.engine in question_hits:
            raise ValueError("Duplicate question/engine matrix row")
        question_hits[row.engine] = row.hit
    return hits


def build(rows: Iterable[MatrixRow | Mapping[str, Any]], questions: Sequence[Question]) -> FirstEngineTable:
    """Build fair per-category hit rates, refusing incomplete candidate cohorts."""
    hits = _learn_hits(rows, questions)
    engines = list(config.ENGINES)
    if not engines or len(engines) != len(set(engines)):
        raise ValueError("ENGINES must contain unique candidate engines")
    prices = {engine: config.PRICE_USD.get(engine, config.DEFAULT_PRICE_USD) for engine in engines}
    if any(not math.isfinite(price) or price < 0 for price in prices.values()):
        raise ValueError("Engine prices must be finite and nonnegative")
    missing = sum(engine not in question_hits for question_hits in hits.values() for engine in engines)
    if missing:
        raise ValueError(f"Incomplete learn matrix: {missing} missing question/engine rows; finish collection first")
    counts: dict[Category, dict[str, EngineCounts]] = {}
    routing: dict[Category, str] = {}
    for category in get_args(Category):
        cohort = [question.id for question in questions if question.category == category]
        if not cohort:
            raise ValueError(f"No learn questions for category {category}")
        counts[category] = {}
        for engine in engines:
            n_hits = sum(hits[key][engine] for key in cohort)
            counts[category][engine] = EngineCounts(n_questions=len(cohort), hits=n_hits, hit_rate=n_hits / len(cohort))
        routing[category] = min(engines, key=lambda engine: (-counts[category][engine].hits, prices[engine], engine))
    return FirstEngineTable(built_on_split="learn", n_questions=len(questions), routing=routing, counts=counts, price_usd=prices)


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path.name} at line {line_number}") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"Expected an object in {path.name} at line {line_number}")
            yield raw


def load_learn_inputs() -> tuple[list[Question], list[MatrixRow]]:
    """Filter shared files by split metadata before inspecting text or hit labels.

    Shared matrix rows have no split field, so join their IDs to question split
    metadata. Known held-out IDs are skipped; unknown IDs fail closed. The
    builder itself refuses non-learn inputs rather than filtering them.
    """
    questions: list[Question] = []
    excluded: set[str] = set()
    seen: set[str] = set()
    for path in (config.FIRST_ENGINE_QUESTIONS_PATH, config.FIRST_ENGINE_TRAPS_PATH):
        for raw in _jsonl(Path(path)):
            question_id = raw.get("id")
            if not isinstance(question_id, str) or not question_id or question_id in seen:
                raise ValueError("Question metadata requires unique nonempty IDs")
            if raw.get("split") not in {"learn", "test"}:
                raise LearnOnlyError("Question metadata must explicitly identify learn or test split")
            seen.add(question_id)
            if raw["split"] != "learn" or raw.get("is_trap", False) or raw.get("category") == "trap":
                excluded.add(question_id)
                continue
            questions.append(Question.model_validate(raw))
    learn_ids = set(_learn_index(questions))
    rows: list[MatrixRow] = []
    for raw in _jsonl(Path(config.FIRST_ENGINE_MATRIX_PATH)):
        question_id = raw.get("question_id")
        if question_id in excluded:
            continue
        if question_id not in learn_ids:
            raise LearnOnlyError("Matrix contains an unknown question ID; supply matching question metadata")
        # Check explicit metadata before MatrixRow discards additional fields.
        if raw.get("split", "learn") != "learn" or raw.get("is_trap", False) or raw.get("category") == "trap":
            raise LearnOnlyError("Refusing test or trap labels on a learn matrix row")
        rows.append(MatrixRow.model_validate(raw, strict=True))
    return questions, rows


def _simulate(hits: Mapping[str, bool], first: str, rescue_table: RescueTable) -> tuple[int, bool]:
    tried: list[str] = []
    engine = first
    while True:
        if engine not in hits:
            raise ValueError(f"Learn simulation lacks an observed result for engine {engine!r}")
        tried.append(engine)
        hit = hits[engine]
        verdict = CheckerVerdict(verdict="HIT" if hit else "DEAD", confidence=1, why="Matrix oracle")
        action = next_action(verdict, tried, len(tried) - 1, rescue_table)
        if action.kind in {"HIT", "NOT_FOUND"}:
            return len(tried), hit
        if action.kind != "RETRY_ENGINE" or action.engine is None or action.engine in tried:
            raise ValueError("Matrix simulation requires nonrepeating engine retries")
        engine = action.engine


def measure(
    rows: Sequence[MatrixRow], questions: Sequence[Question],
    table: FirstEngineTable, rescue_table: RescueTable,
) -> dict[str, Any]:
    """Measure text classification and first-call coverage on learn questions only."""
    hits = _learn_hits(rows, questions)
    if table != build(rows, questions):
        raise ValueError("Measurement table does not match the supplied learn evidence and prices")
    predictions = {question.id: classify(question.question, "") for question in questions}
    result: dict[str, Any] = {
        "split": "learn", "n_questions": len(questions), "baseline_engine": config.FIRST_ENGINE,
        "routing": table.routing, "classifier_input": "question text; empty goal (dataset has no goals)",
        "simulation": "Perfect HIT/DEAD checker, retry-only policy; no page reads, rewrites, errors, cost or time limits",
        "max_rescues": config.MAX_RESCUES, "rescue_order_after": rescue_table.order_after,
        "rescue_fallback_order": rescue_table.fallback_order, "categories": {},
    }
    for category in (*get_args(Category), "overall"):
        cohort = [q for q in questions if category == "overall" or q.category == category]
        correct = sum(predictions[q.id] == q.category for q in cohort)
        block: dict[str, Any] = {
            "n_questions": len(cohort), "classifier_correct": correct, "classifier_accuracy": correct / len(cohort),
        }
        for name in ("baseline", "routed"):
            first_hits = calls = found = 0
            for question in cohort:
                first = config.FIRST_ENGINE if name == "baseline" else table.routing[predictions[question.id]]
                count, hit = _simulate(hits[question.id], first, rescue_table)
                first_hits += hits[question.id][first]
                calls += count
                found += hit
            block[name] = {
                "first_call_hits": first_hits, "first_call_hit_rate": first_hits / len(cohort),
                "simulated_engine_calls": calls, "simulated_engine_calls_per_question": calls / len(cohort),
                "simulated_found": found,
            }
        result["categories"][category] = block
    return result


def write_table(table: FirstEngineTable, path: Path) -> None:
    """Replace the table atomically so concurrent searches never read partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(table.model_dump_json(indent=2) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "measure"))
    parser.add_argument("--split", choices=("learn",), default="learn")
    args = parser.parse_args()
    try:
        questions, rows = load_learn_inputs()
        table = build(rows, questions)
        report = measure(rows, questions, table, load_rescue_table())
        if args.command == "build":
            write_table(table, Path(config.FIRST_ENGINE_TABLE_PATH))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"First-engine routing failed: {exc}\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
