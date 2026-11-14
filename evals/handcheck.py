"""Human check of the hit judge.

Samples 30 matrix rows (seed 14, balanced between judge hits and misses),
shows the question, gold and the exact results the judge saw, and asks you:
"Does any title or snippet contain the gold answer?" Writes agreement to
out/judge_agreement.json. Resumable: answers are saved after every row.

  python -m evals.handcheck            # label
  python -m evals.handcheck --report   # print agreement only
"""
from __future__ import annotations

import argparse
import json
import random
import textwrap

from data.load import load_all
from evals import deps
from evals.contamination import filter_results
from evals.matrix import _read_jsonl, matrix_path, results_path

N = 30
SEED = 14


def _sample() -> list[dict]:
    rows = [r for r in _read_jsonl(matrix_path())
            if not r["question_id"].startswith("trap-") and not r.get("engine_error")]
    rng = random.Random(SEED)
    hits = [r for r in rows if r["hit"]]
    misses = [r for r in rows if not r["hit"]]
    rng.shuffle(hits)
    rng.shuffle(misses)
    k = min(N // 2, len(hits))
    sample = hits[:k] + misses[: N - k]
    rng.shuffle(sample)
    return sample


def agreement_path():
    return deps.OUT / "judge_agreement.json"


def _load_state(labeler: str) -> dict:
    path = agreement_path()
    if path.exists():
        state = json.loads(path.read_text())
        if state.get("labeler", labeler) != labeler:
            raise SystemExit(f"{path} was labelled by {state['labeler']!r}; "
                             f"move it aside before labelling as {labeler!r}.")
        return state
    return {"n_checked": 0, "agree": 0, "labeler": labeler, "labels": []}


def _save(state: dict) -> None:
    labels = state["labels"]
    state["n_checked"] = len(labels)
    state["agree"] = sum(1 for l in labels if l["labeler_hit"] == l["judge_hit"])
    state["agreement_rate"] = round(state["agree"] / state["n_checked"], 3) if labels else None
    agreement_path().write_text(json.dumps(state, indent=2))


def report(state: dict) -> None:
    n, a = state["n_checked"], state["agree"]
    print(f"judge agreement ({state.get('labeler', 'human')}): {a}/{n}" + (f" = {a / n:.0%}" if n else ""))
    fp = sum(1 for l in state["labels"] if l["judge_hit"] and not l["labeler_hit"])
    fn = sum(1 for l in state["labels"] if not l["judge_hit"] and l["labeler_hit"])
    print(f"  judge said hit, labeler said miss: {fp}\n  judge said miss, labeler said hit: {fn}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--report", action="store_true")
    p.add_argument("--labeler", default="human",
                   help='who labels: "human" (default) or e.g. "model:claude-opus-5-5"; stored in the output')
    a = p.parse_args()
    state = _load_state(a.labeler)
    if a.report:
        report(state)
        return
    qs = {q.id: q for q in load_all()}
    results = {(r["question_id"], r["engine"]): r["results"] for r in _read_jsonl(results_path())}
    done = {(l["question_id"], l["engine"]) for l in state["labels"]}
    sample = [r for r in _sample() if (r["question_id"], r["engine"]) not in done]
    print(f"{len(done)} already labelled, {len(sample)} to go. Answer y / n (s = skip, q = quit).\n")
    for i, row in enumerate(sample, len(done) + 1):
        q = qs[row["question_id"]]
        print("=" * 78)
        print(f"[{i}/{N}] {q.id}  engine={row['engine']}")
        print(textwrap.fill("QUESTION: " + q.question, 78))
        print("GOLD:     " + " | ".join(q.gold))
        print("-" * 78)
        shown, _ = filter_results(q.question, results.get((q.id, row["engine"]), []))  # what the judge saw
        for j, r in enumerate(shown, 1):
            print(textwrap.fill(f"[{j}] {r.get('title') or ''}", 78))
            print(textwrap.indent(textwrap.fill(r.get("snippet") or "", 74), "    "))
        print("-" * 78)
        while True:
            ans = input("Does any title or snippet contain the gold answer? [y/n/s/q] ").strip().lower()
            if ans in ("y", "n", "s", "q"):
                break
        if ans == "q":
            break
        if ans == "s":
            continue
        state["labels"].append({"question_id": q.id, "engine": row["engine"],
                                "judge_hit": bool(row["hit"]), "labeler_hit": ans == "y"})
        _save(state)
    _save(state)
    report(state)


if __name__ == "__main__":
    main()
