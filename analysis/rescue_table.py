"""Rescue table (lane S3), section 6.6. Learn split only.

rescue[A][B] = questions where A missed and B hit, over misses(A).
order_after[A] sorts the other engines by rate desc, ties by lower price.

  python -m analysis.rescue_table
"""
from __future__ import annotations

import json

from data.load import load_all
from so.contracts import RescueTable

from analysis.common import ENGINES, FIRST_ENGINE, OUT, hits_by_question, need_matrix, price


class LearnOnlyError(RuntimeError):
    """Raised when a test or trap row would be used to build the rescue table."""


def build(rows: list[dict], questions: list, engines: list[str] = ENGINES) -> dict:
    """Build the table from matrix rows. `questions` maps ids to split and trap flag.

    Raises LearnOnlyError if any row belongs to a test or trap question.
    """
    info = {q.id: q for q in questions}
    bad = sorted({r["question_id"] for r in rows
                  if r["question_id"] in info
                  and (info[r["question_id"]].split != "learn" or info[r["question_id"]].is_trap)})
    if bad:
        raise LearnOnlyError(f"refusing to use {len(bad)} non-learn rows (test or trap), e.g. {bad[:3]}")
    unknown = {r["question_id"] for r in rows if r["question_id"] not in info}
    if unknown:
        raise LearnOnlyError(f"{len(unknown)} rows have unknown question ids, e.g. {sorted(unknown)[:3]}")
    hits = hits_by_question(rows, set(info))
    rescue: dict[str, dict[str, dict]] = {}
    miss_counts: dict[str, int] = {}
    for a in engines:
        missed = [q for q, h in hits.items() if h.get(a) is False]
        miss_counts[a] = len(missed)
        rescue[a] = {}
        for b in engines:
            if b == a:
                continue
            resc = sum(1 for q in missed if hits[q].get(b) is True)
            rescue[a][b] = {"rescued": resc, "misses": len(missed),
                            "rate": round(resc / len(missed), 4) if missed else 0.0}
    order_after = {a: sorted(rescue[a], key=lambda b: (-rescue[a][b]["rate"], price(b), engines.index(b)))
                   for a in engines}
    return {"built_on_split": "learn", "n_questions": len(hits), "miss_counts": miss_counts,
            "rescue": rescue, "order_after": order_after, "fallback_order": order_after[FIRST_ENGINE]}


def print_table(t: dict) -> None:
    engines = list(t["rescue"])
    print(f"rescue table, learn split, n={t['n_questions']}  (cell = rescued/misses, rate; row = A missed, column = B rescues)")
    print(f"{'A missed':12}" + "".join(f"{b:>16}" for b in engines))
    for a in engines:
        cells = []
        for b in engines:
            c = t["rescue"][a].get(b)
            cells.append(f"{'-':>16}" if c is None else f"{c['rescued']:>3}/{c['misses']:<3} {c['rate']:>5.0%}".rjust(16))
        print(f"{a:12}" + "".join(cells))
    thin = [(a, b) for a in engines for b, c in t["rescue"][a].items() if c["misses"] < 10]
    if thin:
        print(f"thin cells (fewer than 10 misses): {len(thin)}")
    print("order after tavily:", t["fallback_order"])


def main() -> None:
    questions = load_all("learn")  # traps are all test, so none come back
    learn_ids = {q.id for q in questions}
    rows = [r for r in need_matrix() if r["question_id"] in learn_ids]
    t = build(rows, questions)
    RescueTable.model_validate(t)  # section 6.6 contract
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "rescue_table.json").write_text(json.dumps(t, indent=2))
    print_table(t)
    print(f"wrote {OUT / 'rescue_table.json'}")


if __name__ == "__main__":
    main()
