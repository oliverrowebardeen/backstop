"""Load questions and traps as Question objects (lane S1).

Run: python -m data.load --stats
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from so.contracts import Question

DATA = Path(__file__).resolve().parent
QUESTIONS = DATA / "questions.jsonl"
TRAPS = DATA / "traps.jsonl"


def _read(path: Path) -> list[Question]:
    with path.open() as f:
        return [Question.model_validate(json.loads(line)) for line in f if line.strip()]


def load_questions(split: str | None = None, category: str | None = None) -> list[Question]:
    qs = _read(QUESTIONS)
    if split is not None:
        qs = [q for q in qs if q.split == split]
    if category is not None:
        qs = [q for q in qs if q.category == category]
    return qs


def load_traps() -> list[Question]:
    return _read(TRAPS)


def load_all(split: str | None = None) -> list[Question]:
    """Questions plus traps. Traps are all test, so split='learn' returns none."""
    qs = load_questions(split=split) + [t for t in load_traps() if split in (None, t.split)]
    return qs


def stats() -> None:
    qs = load_questions()
    traps = load_traps()
    print(f"questions: {len(qs)}")
    print("  by split:   ", dict(sorted(Counter(q.split for q in qs).items())))
    print("  by category:", dict(sorted(Counter(q.category for q in qs).items())))
    cells = Counter((q.split, q.category) for q in qs)
    for (s, c), n in sorted(cells.items()):
        print(f"    {s:5} {c:8} {n}")
    print(f"traps: {len(traps)}")
    print("  by split:   ", dict(Counter(t.split for t in traps)))
    ids = [q.id for q in qs + traps]
    assert len(ids) == len(set(ids)), "duplicate question ids"
    assert all(t.is_trap and t.category == "trap" and t.trap_note for t in traps)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--stats", action="store_true")
    a = p.parse_args()
    stats() if a.stats else p.print_help()
