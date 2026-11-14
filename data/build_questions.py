"""Build data/questions.jsonl from public sets.

230 SimpleQA questions (category fact) and 70 FinanceBench questions
(category finance), split 100 learn / 200 test, stratified by category,
seed 14. If a source cannot be reached, it says so and fills from the other.

Run: python -m data.build_questions
"""
from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import random
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "questions.jsonl"
CACHE = ROOT / "cache" / "datasets"

SIMPLEQA_URL = "https://openaipublic.blob.core.windows.net/simple-evals/simple_qa_test_set.csv"
FINANCEBENCH_URL = (
    "https://raw.githubusercontent.com/patronus-ai/financebench/main/data/"
    "financebench_open_source.jsonl"
)

SEED = 14
N_FACT = 230
N_FINANCE = 70
N_LEARN = 100
N_TEST = 200

# Questions whose answer moves with today's date.
DATE_DEPENDENT = re.compile(
    r"\b(current|currently|as of (today|now)|latest|most recent|nowadays|"
    r"right now|this year|today|still|present day|so far|to date)\b",
    re.I,
)
# Finance version: "current ratio" and "current assets" are metrics, not dates.
FIN_DATE_DEPENDENT = re.compile(
    r"\b(currently|as of (today|now)|latest|most recent|right now|today|to date)\b", re.I
)
# Gold answers we cannot check against a snippet.
UNCHECKABLE_GOLD = re.compile(
    r"^(yes|no)\b|there are none|experienced a|coverage ratio is zero", re.I
)
# Persona and instruction sentences in FinanceBench prompts. They are noise for
# a search query; the question sentence itself is kept.
FB_BOILERPLATE = re.compile(
    r"^(assume|you are|answer the following|address the question|base your|basing your|"
    r"by drawing|considering|in agreement|using|please|give a response|compute or extract|"
    r"respond to|answer the question|according to the information|calculate what was asked|"
    r"utilizing|by relying|we need|we want|from the perspective|based on the information|"
    r"approach the question|round your answer|reply to|provide a response|use|calculate)\b",
    re.I,
)


def _fetch(url: str, name: str) -> str | None:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    if path.exists():
        return path.read_text()
    try:
        r = httpx.get(url, timeout=60, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"!! SOURCE UNREACHABLE: {name} ({url}): {e}", file=sys.stderr)
        return None
    path.write_text(r.text)
    return r.text


def _aliases(answer: str) -> list[str]:
    answer = answer.strip()
    gold = [answer]
    stripped = re.sub(r"\s*\(.*?\)\s*", " ", answer).strip()
    if stripped and stripped != answer:
        gold.append(stripped)
    return gold


def simpleqa_candidates(text: str) -> list[dict]:
    out = []
    for i, row in enumerate(csv.DictReader(io.StringIO(text))):
        q, a = row["problem"].strip(), row["answer"].strip()
        if not a or len(a) > 40 or DATE_DEPENDENT.search(q):
            continue
        try:
            meta = ast.literal_eval(row["metadata"])
        except Exception:  # noqa: BLE001
            meta = {}
        out.append({
            "id": f"sqa-{i + 1:04d}",
            "question": q,
            "gold": _aliases(a),
            "category": "fact",
            "source": "simpleqa",
            "_topic": meta.get("topic"),
        })
    return out


def _clean_fb_question(q: str) -> str:
    """Drop persona sentences; keep the question, definitions and units."""
    sentences = re.split(r"(?<=[.?!])\s+", q.strip())
    keep_markers = re.compile(r"\?|defined as|\bdefine\b|answer in|in usd|units of", re.I)
    kept = []
    for s in sentences:
        if "?" in s:
            # "By drawing conclusions from X, what is Y?" -> "What is Y?"
            m = re.search(r",\s*(what|how|which|who|when|does|did|is|was)\b", s, re.I)
            if m and FB_BOILERPLATE.match(s):
                s = s[m.start(1):]
                s = s[0].upper() + s[1:]
            s = re.sub(r"^here's the question:\s*", "", s, flags=re.I)
            s = s[0].upper() + s[1:]
            kept.append(s)
        elif keep_markers.search(s) or not FB_BOILERPLATE.match(s):
            kept.append(s)
    cleaned = " ".join(kept).strip()
    return cleaned if "?" in cleaned else q.strip()


def financebench_candidates(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        a = r["answer"].strip()
        if len(a) > 60 or UNCHECKABLE_GOLD.search(a) or FIN_DATE_DEPENDENT.search(r["question"]):
            continue
        if not re.search(r"\d", a) and len(a.split()) > 5:
            continue  # a sentence of judgment, not a checkable value
        gold = _aliases(a)
        # "$1577.00" style: also accept the bare figure.
        m = re.fullmatch(r"\$?(-?[\d,]+)\.00", a)
        if m:
            gold.append(m.group(1))
        out.append({
            "id": f"fb-{r['financebench_id'].rsplit('_', 1)[-1]}",
            "question": _clean_fb_question(r["question"]),
            "gold": gold,
            "category": "finance",
            "source": "financebench",
            "_company": r["company"],
        })
    return out


def stratified_split(rows: list[dict], n_learn: int, rng: random.Random) -> None:
    """Assign split in place: n_learn learn rows, proportional per category."""
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    total = len(rows)
    quotas = {c: n_learn * len(v) / total for c, v in by_cat.items()}
    alloc = {c: int(q) for c, q in quotas.items()}
    # Largest remainder, ties by category name for determinism.
    for c in sorted(quotas, key=lambda c: (-(quotas[c] - alloc[c]), c))[: n_learn - sum(alloc.values())]:
        alloc[c] += 1
    for c in sorted(by_cat):
        items = sorted(by_cat[c], key=lambda r: r["id"])
        rng.shuffle(items)
        for j, r in enumerate(items):
            r["split"] = "learn" if j < alloc[c] else "test"


def build(seed: int = SEED, n_fact: int = N_FACT, n_finance: int = N_FINANCE,
          n_learn: int = N_LEARN, n_test: int = N_TEST) -> list[dict]:
    rng = random.Random(seed)
    sqa_text = _fetch(SIMPLEQA_URL, "simple_qa_test_set.csv")
    fb_text = _fetch(FINANCEBENCH_URL, "financebench_open_source.jsonl")
    if sqa_text is None and fb_text is None:
        sys.exit("Both sources unreachable. Cannot build questions.")

    fact_pool = simpleqa_candidates(sqa_text) if sqa_text else []
    fin_pool = financebench_candidates(fb_text) if fb_text else []
    rng.shuffle(fact_pool)
    rng.shuffle(fin_pool)

    finance = fin_pool[:n_finance]
    n_fact_total = n_fact + (n_finance - len(finance))
    if len(finance) < n_finance:
        print(f"!! FinanceBench gave {len(finance)} usable questions, not {n_finance}. "
              f"Filling {n_finance - len(finance)} from SimpleQA.", file=sys.stderr)
    fact = fact_pool[:n_fact_total]
    if len(fact) < n_fact_total:
        short = n_fact_total - len(fact)
        print(f"!! SimpleQA short by {short}. Filling from FinanceBench.", file=sys.stderr)
        finance += fin_pool[n_finance:n_finance + short]

    rows = fact + finance
    if len(rows) != n_learn + n_test:
        sys.exit(f"Only {len(rows)} usable questions, need {n_learn + n_test}.")
    stratified_split(rows, n_learn, rng)

    out = []
    for r in sorted(rows, key=lambda r: r["id"]):
        out.append({
            "id": r["id"], "question": r["question"], "gold": r["gold"],
            "category": r["category"], "source": r["source"], "split": r["split"],
            "is_trap": False, "trap_note": None,
        })
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--n-fact", type=int, default=N_FACT)
    p.add_argument("--n-finance", type=int, default=N_FINANCE)
    p.add_argument("--n-learn", type=int, default=N_LEARN)
    p.add_argument("--n-test", type=int, default=N_TEST)
    a = p.parse_args()
    rows = build(a.seed, a.n_fact, a.n_finance, a.n_learn, a.n_test)
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"wrote {len(rows)} questions to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
