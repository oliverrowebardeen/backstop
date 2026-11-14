"""Shared helpers for lane S3 (rescue table, frontier, heatmap, findings).

OUT is where the analysis reads and writes artifacts (out/ by default). It is a
plain module attribute, read at call time here and in the other analysis modules,
so callers can point it elsewhere. Kept apart from evals.deps on purpose
(evals.deps imports so.llm).
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from data.load import load_all

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"

from so.config import (DEFAULT_PRICE_USD, ENGINES, FALLBACK_ORDER, FIRST_ENGINE,  # noqa: E402,F401
                       PRICE_USD)

# The spec's fixed fallback (section 6.6/6.8), the baseline for "learned vs fixed".
FIXED_ORDER = list(FALLBACK_ORDER)


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
        raise SystemExit(f"no matrix rows at {OUT / 'matrix.jsonl'} (run python -m evals.matrix)")
    return rows


def load_rescue_table() -> dict:
    return json.loads((OUT / "rescue_table.json").read_text())
