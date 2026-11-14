"""Reproducible learn-only measurements using the real agent and answer judge."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import json
from pathlib import Path
import random
from typing import Any

from data.load import load_questions
from evals.judge import SPEND, grade_answer
from so import config
from so.agent import run_question
from so.contracts import Question
from so.eventlog import read_log, summarize


def cohort(mode: str, matrix: Path) -> list[Question]:
    """Freeze selection before running models; never return test or trap rows."""
    questions = sorted(load_questions(split="learn"), key=lambda question: question.id)
    assert all(question.split == "learn" and not question.is_trap for question in questions)
    if mode == "finance":
        learn_ids = {question.id for question in questions}
        rows: dict[str, dict[str, bool]] = defaultdict(dict)
        for line in matrix.read_text().splitlines():
            row = json.loads(line)
            if row["question_id"] not in learn_ids:
                continue
            rows[row["question_id"]][row["engine"]] = row["hit"]
        return [question for question in questions if question.category == "finance"
                and set(config.ENGINES) <= rows[question.id].keys()
                and not any(rows[question.id][engine] for engine in config.ENGINES)]
    return random.Random(14).sample(questions, 40)


async def measure(args: argparse.Namespace) -> dict[str, Any]:
    questions = cohort(args.mode, args.matrix)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {"split": "learn", "mode": args.mode, "ids": [question.id for question in questions],
                "finance_deep_read": args.finance_deep_read,
                "evidence_filter_enabled": config.EVIDENCE_FILTER_ENABLED,
                "evidence_extra_results": config.EVIDENCE_EXTRA_RESULTS,
                "first_engine_routing": config.FIRST_ENGINE_ROUTING,
                "extract_chunks": config.FINANCE_EXTRACT_CHUNKS,
                "search_timeout_s": config.SEARCH_TIMEOUT_S,
                "checker_timeout_s": config.CHECKER_TIMEOUT_S,
                "max_cost_usd": config.SEARCH_MAX_COST_USD,
                "models": {role: getattr(config, f"{role.upper()}_MODEL")
                           for role in ("agent", "checker", "judge")}}
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        raise ValueError("Existing measurement has a different cohort or model configuration")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    semaphore = asyncio.Semaphore(args.concurrency)

    async def one(question: Question) -> dict[str, Any]:
        directory = args.output / question.id
        result_path = directory / "result.json"
        if result_path.exists():
            return json.loads(result_path.read_text())
        directory.mkdir(exist_ok=True)
        if list(directory.glob("*.jsonl")):
            raise ValueError(f"Interrupted measurement for {question.id}; use a fresh output directory")
        async with semaphore:
            row: dict[str, Any] = {"question_id": question.id}
            try:
                answer = await run_question(question, "second_opinion", log_dir=directory)
                row.update(answer=answer.model_dump(), grade=await grade_answer(question, answer))
            except Exception as exc:
                row.update(error=type(exc).__name__, grade="error")
            logs = list(directory.glob("*.jsonl"))
            events = read_log(logs[0]) if logs else []
            row["totals"] = summarize(logs[0]) if logs else {}
            usage = [event["data"] for event in events if event["type"] == "llm"]
            row["tokens_in"] = sum(item.get("tokens_in", 0) for item in usage)
            row["tokens_out"] = sum(item.get("tokens_out", 0) for item in usage)
            row["checks"] = dict(Counter(event["data"].get("verdict") for event in events
                                         if event["type"] == "check"))
            row["deep_read_hit"] = any(
                event["type"] == "search_done" and event["data"].get("status") == "HIT"
                and event["data"].get("calls") == 1
                and event["data"].get("evidence", {}).get("source") == "tavily_extract"
                and event["data"]["evidence"]["url"] in row.get("answer", {}).get("citations", [])
                for event in events
            )
            row["recovered"] = row["grade"] == "correct" and row["deep_read_hit"]
            result_path.write_text(json.dumps(row, indent=2) + "\n")
            print(json.dumps({key: row[key] for key in ("question_id", "grade", "deep_read_hit", "recovered")}), flush=True)
            return row

    rows = await asyncio.gather(*(one(question) for question in questions))
    report = {**manifest, "n": len(rows), "grades": dict(Counter(row["grade"] for row in rows)),
              "recovered": sum(row["recovered"] for row in rows),
              "tokens_in": sum(row["tokens_in"] for row in rows),
              "tokens_out": sum(row["tokens_out"] for row in rows),
              "cost_usd": sum(row["totals"].get("cost_usd_total", 0) for row in rows),
              "judge_spend": dict(SPEND)}
    (args.output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("finance", "evidence"))
    parser.add_argument("--matrix", type=Path, default=config.OUT_DIR / "matrix.jsonl")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=config.LLM_MAX_CONCURRENCY)
    parser.add_argument("--finance-deep-read", action=argparse.BooleanOptionalAction,
                        default=config.FINANCE_DEEP_READ)
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    config.FINANCE_DEEP_READ = args.finance_deep_read
    asyncio.run(measure(args))


if __name__ == "__main__":
    main()
