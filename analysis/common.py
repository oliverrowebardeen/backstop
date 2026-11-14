"""Shared helpers for lane S3 (rescue table, frontier, heatmap, findings).

Paths: out/fake/ when SO_FAKE=1, else out/. Replicates the tiny OUT logic from
evals/deps.py on purpose (evals.deps imports so.llm).
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from data.load import load_all

ROOT = Path(__file__).resolve().parent.parent
FAKE = os.getenv("SO_FAKE") == "1"
OUT = ROOT / "out" / "fake" if FAKE else ROOT / "out"

try:
    from so.config import ENGINES, FIRST_ENGINE  # type: ignore
except Exception:  # so/ not merged yet
    ENGINES = ["tavily", "exa", "brave", "perplexity", "parallel", "ceramic"]
    FIRST_ENGINE = "tavily"

# Section 6.2
try:
    from so.config import DEFAULT_PRICE_USD, PRICE_USD  # type: ignore
except Exception:
    PRICE_USD = {"tavily": 0.005, "brave": 0.005, "you": 0.005,
                 "parallel": 0.001, "ceramic": 0.00005, "tinyfish": 0.0}
    DEFAULT_PRICE_USD = 0.005

# The spec's fixed fallback (section 6.6), used as the "fixed order" baseline.
try:
    from so.config import FALLBACK_ORDER as FIXED_ORDER  # type: ignore
except Exception:
    FIXED_ORDER = ["exa", "perplexity", "brave", "parallel", "ceramic"]


def price(engine: str) -> float:
    return PRICE_USD.get(engine, DEFAULT_PRICE_USD)


def read_jsonl(path: Path) -> list[dict]:
    """Tolerant: missing file gives [], bad lines are skipped."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and "question_id" in row and "engine" in row:
            rows.append(row)
    return rows


def hits_by_question(rows: list[dict], ids: set[str]) -> dict[str, dict[str, bool]]:
    """question_id -> engine -> hit, restricted to ids. Last row per pair wins."""
    out: dict[str, dict[str, bool]] = defaultdict(dict)
    for r in rows:
        if r["question_id"] in ids:
            out[r["question_id"]][r["engine"]] = bool(r.get("hit"))
    return dict(out)


def split_ids(split: str, *, traps: bool = False) -> set[str]:
    return {q.id for q in load_all(split) if bool(q.is_trap) == traps}


def need_matrix() -> list[dict]:
    rows = read_jsonl(OUT / "matrix.jsonl")
    if not rows:
        raise SystemExit(f"no matrix rows at {OUT / 'matrix.jsonl'} "
                         "(fake data: SO_FAKE=1 python -m evals.matrix)")
    return rows


def load_rescue_table() -> dict:
    return json.loads((OUT / "rescue_table.json").read_text())


def banner() -> str:
    return "FAKE DATA (SO_FAKE=1)" if FAKE else "real data"
