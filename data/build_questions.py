"""Build data/questions.jsonl from public sets (lane S1).

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


def build() -> list[dict]:
    rng = random.Random(SEED)
    sqa_text = _fetch(SIMPLEQA_URL, "simple_qa_test_set.csv")
    fb_text = _fetch(FINANCEBENCH_URL, "financebench_open_source.jsonl")
    if sqa_text is None and fb_text is None:
        sys.exit("Both sources unreachable. Cannot build questions.")

    fact_pool = simpleqa_candidates(sqa_text) if sqa_text else []
    fin_pool = financebench_candidates(fb_text) if fb_text else []
    rng.shuffle(fact_pool)
    rng.shuffle(fin_pool)

    finance = fin_pool[:N_FINANCE]
    n_fact = N_FACT + (N_FINANCE - len(finance))
    if len(finance) < N_FINANCE:
        print(f"!! FinanceBench gave {len(finance)} usable questions, not {N_FINANCE}. "
              f"Filling {N_FINANCE - len(finance)} from SimpleQA.", file=sys.stderr)
    fact = fact_pool[:n_fact]
    if len(fact) < n_fact:
        short = n_fact - len(fact)
        print(f"!! SimpleQA short by {short}. Filling from FinanceBench.", file=sys.stderr)
        finance += fin_pool[N_FINANCE:N_FINANCE + short]

    rows = fact + finance
    if len(rows) != N_LEARN + N_TEST:
        sys.exit(f"Only {len(rows)} usable questions, need {N_LEARN + N_TEST}.")
    stratified_split(rows, N_LEARN, rng)

    out = []
    for r in sorted(rows, key=lambda r: r["id"]):
        out.append({
            "id": r["id"], "question": r["question"], "gold": r["gold"],
            "category": r["category"], "source": r["source"], "split": r["split"],
            "is_trap": False, "trap_note": None,
        })
    return out


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    rows = build()
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"wrote {len(rows)} questions to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
