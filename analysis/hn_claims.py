"""Test three public engine claims against our hit matrix (lane A4).

    python -m analysis.hn_claims

Uses only out/matrix.jsonl (no new calls). Hit rates and their bootstrap 95%
intervals are on the test split, non-trap questions, as every reported number
must be. The rescue-per-dollar order is built on the learn split, like the
rescue table it is compared with. It is a recommendation only: the rescue
table is not changed.

Claim rules (bootstrap over questions, paired, so every engine is resampled on
the same questions):
- A strength ("strong", "works well") is SUPPORTED when the engine's hit rate
  minus the median of the other five engines' rates has a 95% interval wholly
  above 0, NOT SUPPORTED when it is wholly below 0, INCONCLUSIVE otherwise.
  The median is used so one very weak engine does not flatter the rest.
- "May not be best for X" compares the engine with the best other engine on
  that subset (best by point estimate); SUPPORTED when the interval is wholly
  below 0.
- "Extremely cheap" is read off the price table: at most a tenth of the median
  other engine's price.
- A claim with several parts is NOT SUPPORTED if any part is, SUPPORTED if all
  parts are, otherwise INCONCLUSIVE. A part with no questions is INCONCLUSIVE.

Writes out/hn_claims.json and out/hn_claims.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from analysis.stats import INCONCLUSIVE, NOT_SUPPORTED, SUPPORTED, bootstrap_mean_ci, combine_labels, label_direction
from so.config import DEFAULT_PRICE_USD, ENGINES, FIRST_ENGINE, OUT_DIR, PRICE_USD

logger = logging.getLogger(__name__)

SEED = 14
N_BOOT = 10_000
OUT_JSON = OUT_DIR / "hn_claims.json"
OUT_MD = OUT_DIR / "hn_claims.md"
RECENT = re.compile(r"(?<!\d)202[56](?!\d)")
CHEAP_FACTOR = 0.1

# Telem latency-study list prices per call. Perplexity's was not published there.
LIST_PRICE_USD: dict[str, float | None] = {"ceramic": 0.00005, "brave": 0.005, "parallel": 0.005,
                                           "exa": 0.007, "tavily": 0.008, "perplexity": None}

TELEM_CONTEXT = ("Telem's own published numbers, not ours: Telem's Oct 22 2026 post reported that Tavily found "
                 "known answers on 244 of 363 questions and that Tavily plus Exa top-5 found 279.")

CLAIMS: list[dict[str, Any]] = [
    {"id": "ceramic", "engine": "ceramic",
     "text": "Ceramic is extremely cheap and works well for broad, preliminary searches",
     "parts": [{"what": "extremely cheap", "kind": "price"},
               {"what": "works well (overall hit rate)", "kind": "vs_median", "subset": "overall",
                "direction": "above"}],
     "caveat": "Our questions are specific fact and finance lookups, not broad preliminary searches."},
    {"id": "brave", "engine": "brave",
     "text": "Brave has strong general coverage but may not be best for domain-specific queries like "
             "financial research",
     "parts": [{"what": "strong general coverage (overall hit rate)", "kind": "vs_median", "subset": "overall",
                "direction": "above"},
               {"what": "not best on finance", "kind": "vs_best", "subset": "finance", "direction": "below"}],
     "caveat": None},
    {"id": "exa", "engine": "exa",
     "text": "Exa is strong for entity and people search but can be bad at latest news",
     "parts": [{"what": "strong for entity search (fact questions as proxy)", "kind": "vs_median",
                "subset": "fact", "direction": "above"},
               {"what": "bad at latest news (questions mentioning 2025 or 2026)", "kind": "vs_median",
                "subset": "recent_2025_2026", "direction": "below"}],
     "caveat": "We have no entity or people category; SimpleQA fact questions stand in for entity search. "
               "No test question mentions 2025 or 2026, so the news part cannot be tested on this matrix."},
]


class MatrixIncomplete(RuntimeError):
    """Raised when no question has a matrix row for every engine."""


# ---------------------------------------------------------------- data

def read_matrix(path: Path) -> dict[str, dict[str, bool]]:
    """question_id -> engine -> hit. Later rows win."""
    out: dict[str, dict[str, bool]] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.setdefault(r["question_id"], {})[r["engine"]] = bool(r["hit"])
    return out


def mentions_recent(question: Any) -> bool:
    """True if the question text or any gold answer mentions 2025 or 2026."""
    return bool(RECENT.search(question.question) or any(RECENT.search(g) for g in question.gold))


def subsets(questions: Sequence[Any]) -> dict[str, list[str]]:
    """The four question subsets the claims are judged on."""
    return {
        "overall": [q.id for q in questions],
        "finance": [q.id for q in questions if q.category == "finance"],
        "fact": [q.id for q in questions if q.category == "fact"],
        "recent_2025_2026": [q.id for q in questions if mentions_recent(q)],
    }


def hit_array(hits: Mapping[str, Mapping[str, bool]], qids: Sequence[str], engines: Sequence[str]) -> np.ndarray:
    """(questions x engines) 0/1 array over questions that have a row for every engine."""
    rows = [[hits[q][e] for e in engines] for q in qids if q in hits and all(e in hits[q] for e in engines)]
    return np.asarray(rows, dtype=float).reshape(len(rows), len(engines))


# ---------------------------------------------------------------- statistics

def rate_table(arr: np.ndarray, engines: Sequence[str], rng: np.random.Generator,
               n_boot: int = N_BOOT) -> dict[str, dict[str, Any]]:
    """Per-engine hit count, rate and bootstrap 95% CI."""
    out = {}
    for j, e in enumerate(engines):
        col = arr[:, j] if arr.size else np.array([])
        ci = bootstrap_mean_ci(col, rng, n_boot)
        out[e] = {"n": int(col.size), "hits": int(col.sum()), "rate": round(float(col.mean()), 4) if col.size else None,
                  "ci95": None if ci is None else [round(ci[0], 4), round(ci[1], 4)]}
    return out


def paired_diff_ci(arr: np.ndarray, j: int, comparator: Callable[[np.ndarray], np.ndarray],
                   rng: np.random.Generator, n_boot: int = N_BOOT) -> tuple[float, tuple[float, float]] | None:
    """Point estimate and 95% bootstrap CI of rate[j] - comparator(rates of the others).

    comparator maps a (samples x others) array of rates to one value per sample.
    """
    if arr.shape[0] == 0:
        return None
    others = [k for k in range(arr.shape[1]) if k != j]

    def stat(rates: np.ndarray) -> np.ndarray:
        return rates[:, j] - comparator(rates[:, others])

    point = float(stat(arr.mean(axis=0, keepdims=True))[0])
    idx = rng.integers(0, arr.shape[0], size=(n_boot, arr.shape[0]))
    boots = stat(arr[idx].mean(axis=1))
    return point, (float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975)))


def median_of_others(rates: np.ndarray) -> np.ndarray:
    """Median across the other engines, per bootstrap sample."""
    return np.median(rates, axis=1)


def price_label(engine: str, prices: Mapping[str, float | None], engines: Sequence[str]) -> tuple[str, str]:
    """(label, detail) for 'extremely cheap': at most CHEAP_FACTOR of the median other price."""
    own = prices.get(engine)
    others = [p for e in engines if e != engine and (p := prices.get(e)) is not None]
    if own is None or not others:
        return INCONCLUSIVE, "price unknown"
    med = float(np.median(others))
    ok = own <= CHEAP_FACTOR * med
    return (SUPPORTED if ok else NOT_SUPPORTED), f"{usd(own)} per call vs median other {usd(med)} ({own / med:.3f}x)"


def judge_part(part: Mapping[str, Any], engine: str, arrays: Mapping[str, np.ndarray], engines: Sequence[str],
               prices: Mapping[str, Mapping[str, float | None]], rng: np.random.Generator,
               n_boot: int = N_BOOT) -> dict[str, Any]:
    """Label one part of a claim."""
    if part["kind"] == "price":
        details = {name: price_label(engine, table, engines) for name, table in prices.items()}
        labels = [lab for lab, _ in details.values()]
        return {**part, "label": combine_labels(labels),
                "detail": "; ".join(f"{name}: {d}" for name, (_, d) in details.items())}
    arr = arrays[part["subset"]]
    j = list(engines).index(engine)
    if part["kind"] == "vs_median":
        res = paired_diff_ci(arr, j, median_of_others, rng, n_boot)
        against = "median of the other engines"
    elif part["kind"] == "vs_best":
        if arr.shape[0] == 0:
            res, against = None, "best other engine"
        else:
            rates = arr.mean(axis=0)
            best = max((k for k in range(len(engines)) if k != j), key=lambda k: rates[k])
            others = [k for k in range(len(engines)) if k != j]
            pos = others.index(best)
            res = paired_diff_ci(arr, j, lambda r: r[:, pos], rng, n_boot)
            against = f"best other engine ({engines[best]})"
    else:
        raise ValueError(f"unknown claim part kind {part['kind']!r}")
    if res is None:
        return {**part, "label": INCONCLUSIVE, "n": 0, "against": against,
                "detail": "no questions in this subset"}
    point, ci = res
    return {**part, "label": label_direction(ci, part["direction"]), "n": int(arr.shape[0]), "against": against,
            "diff": round(point, 4), "diff_ci95": [round(ci[0], 4), round(ci[1], 4)]}


def judge_claims(arrays: Mapping[str, np.ndarray], engines: Sequence[str],
                 prices: Mapping[str, Mapping[str, float | None]], rng: np.random.Generator,
                 claims: Sequence[Mapping[str, Any]] = CLAIMS, n_boot: int = N_BOOT) -> list[dict[str, Any]]:
    """Label every claim from its parts."""
    out = []
    for c in claims:
        parts = [judge_part(p, c["engine"], arrays, engines, prices, rng, n_boot) for p in c["parts"]]
        out.append({**c, "parts": parts, "label": combine_labels([p["label"] for p in parts])})
    return out


# ---------------------------------------------------------------- rescues per dollar

def rescues_per_dollar(hits: Mapping[str, Mapping[str, bool]], learn_ids: Sequence[str],
                       prices: Mapping[str, float | None], first: str = FIRST_ENGINE,
                       engines: Sequence[str] = ENGINES) -> dict[str, Any]:
    """For each second engine after a `first` miss on the learn split: rescues per dollar spent on it.

    Cost = calling the second engine on every first-engine miss. Engines with an
    unknown price are listed but left out of the order.
    """
    missed = [q for q in learn_ids if q in hits and hits[q].get(first) is False]
    rows = []
    for e in engines:
        if e == first:
            continue
        rescued = sum(1 for q in missed if hits[q].get(e) is True)
        p = prices.get(e)
        spend = None if p is None else len(missed) * p
        rows.append({"engine": e, "rescued": rescued, "misses": len(missed),
                     "rate": round(rescued / len(missed), 4) if missed else 0.0,
                     "price_usd": p, "spend_usd": None if spend is None else round(spend, 6),
                     "rescues_per_dollar": None if not spend else round(rescued / spend, 2)})
    priced = [r for r in rows if r["rescues_per_dollar"] is not None]
    order = [r["engine"] for r in sorted(priced, key=lambda r: (-r["rescues_per_dollar"], -r["rate"],
                                                                list(engines).index(r["engine"])))]
    return {"first": first, "split": "learn", "misses": len(missed), "rows": rows, "order": order,
            "unpriced": [r["engine"] for r in rows if r["price_usd"] is None]}


# ---------------------------------------------------------------- oracle capture

def oracle_capture(arr_test: np.ndarray, results: Mapping[str, Any] | None,
                   frontier: Mapping[str, Any] | None) -> dict[str, Any]:
    """Share of the all-engine oracle ceiling the second_opinion setup reaches, and cost per captured question.

    Measured from out/results.json when it exists (accuracy as a share of the
    oracle rate; cost per captured = cost_per_correct), else simulated from the
    out/frontier.json Second Opinion point (coverage share; cost per captured =
    cost_per_q / coverage), labelled simulated.
    """
    n = int(arr_test.shape[0])
    oracle_rate = float(arr_test.max(axis=1).mean()) if n else 0.0
    out: dict[str, Any] = {"split": "test", "n_questions": n, "oracle_hits": int(arr_test.max(axis=1).sum()) if n else 0,
                           "oracle_rate": round(oracle_rate, 4)}
    so = ((results or {}).get("setups") or {}).get("second_opinion")
    if so and so.get("n"):
        out["measured"] = {
            "source": "out/results.json" + (f" ({results['note']})" if results.get("note") else ""),
            "accuracy": so["accuracy"], "n": so["n"],
            "share_of_ceiling": round(so["accuracy"] / oracle_rate, 4) if oracle_rate else None,
            "cost_per_captured": so.get("cost_per_correct"),
            "note": "final-answer accuracy over the oracle rate; a correct answer can come from a page read even "
                    "when no engine's top 5 had it, so this is a ratio of rates, not a subset count",
        }
    pt = next((p for p in (frontier or {}).get("points", []) if p.get("kind") == "second_opinion"), None)
    if pt and pt.get("coverage"):
        out["simulated"] = {
            "source": "out/frontier.json second_opinion point" + (" (simulated)" if pt.get("simulated") else ""),
            "label": pt.get("label"), "coverage": pt["coverage"], "cost_per_q": pt["cost_per_q"],
            "share_of_ceiling": round(pt["coverage"] / oracle_rate, 4) if oracle_rate else None,
            "cost_per_captured": round(pt["cost_per_q"] / pt["coverage"], 6),
        }
    out["headline"] = "measured" if "measured" in out else ("simulated" if "simulated" in out else None)
    return out


# ---------------------------------------------------------------- report

def usd(x: float) -> str:
    """Dollar amount without scientific notation, e.g. $0.00005."""
    return "$" + (f"{x:.6f}".rstrip("0").rstrip(".") or "0")


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _ci(ci: Sequence[float] | None) -> str:
    return "n/a" if ci is None else f"{_pct(ci[0])} to {_pct(ci[1])}"


def render_md(doc: Mapping[str, Any]) -> str:
    """Short markdown report."""
    engines = doc["engines"]
    L = ["# Tested the founder's claims", "",
         f"Hit rate = gold answer in an engine's top 5 (the hit matrix). Test split, non-trap, "
         f"{doc['n_test']} questions; bootstrap 95% intervals ({N_BOOT} resamples, seed {SEED}).", "",
         "## Verdicts", ""]
    for c in doc["claims"]:
        L.append(f"- **{c['label']}**: \"{c['text']}\"")
        for p in c["parts"]:
            if "diff" in p:
                L.append(f"  - {p['what']}: {p['label']}. {c['engine']} minus {p['against']} = "
                         f"{100 * p['diff']:+.1f} points (95% CI {100 * p['diff_ci95'][0]:+.1f} to "
                         f"{100 * p['diff_ci95'][1]:+.1f}), n={p['n']}.")
            else:
                L.append(f"  - {p['what']}: {p['label']}. {p.get('detail', '')}.")
        if c.get("caveat"):
            L.append(f"  - Caveat: {c['caveat']}")
    L += ["", "## Hit rates by subset", "", "| subset | n | " + " | ".join(engines) + " |",
          "|---|---|" + "---|" * len(engines)]
    for name, table in doc["rates"].items():
        n = next(iter(table.values()))["n"]
        cells = [f"{_pct(table[e]['rate'])} ({_ci(table[e]['ci95'])})" if table[e]["n"] else "n/a" for e in engines]
        L.append(f"| {name} | {n} | " + " | ".join(cells) + " |")
    if not doc["subset_sizes"]["recent_2025_2026"]:
        L += ["", f"No test question or gold answer mentions 2025 or 2026 (newest year mentioned: "
              f"{doc['newest_year_mentioned']}), so recency cannot be tested here."]
    L += ["", "## Second engine after a Tavily miss, per dollar (learn split, recommendation only)", ""]
    for name, rpd in doc["rescues_per_dollar"].items():
        L += [f"Prices: {name}. {rpd['misses']} Tavily misses.", "",
              "| engine | rescued | rate | price | rescues per $ |", "|---|---|---|---|---|"]
        for r in sorted(rpd["rows"], key=lambda r: rpd["order"].index(r["engine"]) if r["engine"] in rpd["order"] else 99):
            price = "unknown" if r["price_usd"] is None else usd(r["price_usd"])
            per = "n/a" if r["rescues_per_dollar"] is None else f"{r['rescues_per_dollar']:.0f}"
            L.append(f"| {r['engine']} | {r['rescued']}/{r['misses']} | {_pct(r['rate'])} | {price} | {per} |")
        L += ["", f"Per-dollar order: {', '.join(rpd['order'])}"
              + (f" (unpriced: {', '.join(rpd['unpriced'])})" if rpd["unpriced"] else "") + ".", ""]
    cmp = doc["order_comparison"]
    L += [f"Rescue table order_after[\"tavily\"] (by rescue rate): {', '.join(cmp['rescue_table'])}. "
          f"The per-dollar order (primary prices) {'differs' if cmp['differs'] else 'does not differ'}"
          f"{': ' + cmp['why'] if cmp['differs'] else ''}. The rescue table is unchanged; with at most two "
          "rescues per search, a rescue slot is scarce, so rate per slot matters as much as rate per dollar.", ""]
    oc = doc["oracle"]
    L += ["## Share of the all-engine ceiling", "",
          f"Oracle (any of the {len(engines)} engines hit) on the test split: {_pct(oc['oracle_rate'])} "
          f"({oc['oracle_hits']}/{oc['n_questions']})."]
    if "measured" in oc:
        m = oc["measured"]
        cost = "n/a" if m["cost_per_captured"] is None else f"${m['cost_per_captured']:.4f}"
        L.append(f"- Measured ({m['source']}): second_opinion accuracy {_pct(m['accuracy'])} on {m['n']} questions "
                 f"= {_pct(m['share_of_ceiling'])} of the ceiling; cost per captured question {cost}. {m['note']}.")
    if "simulated" in oc:
        s = oc["simulated"]
        L.append(f"- SIMULATED ({s['source']}, {s['label']}): coverage {_pct(s['coverage'])} = "
                 f"{_pct(s['share_of_ceiling'])} of the ceiling; cost per captured question "
                 f"${s['cost_per_captured']:.4f}.")
    L += ["", "## Context", "", TELEM_CONTEXT, ""]
    return "\n".join(L)


def build(matrix_path: Path, results_path: Path, frontier_path: Path, rescue_table_path: Path,
          seed: int = SEED, n_boot: int = N_BOOT) -> dict[str, Any]:
    """Compute every number in the report."""
    from data.load import load_questions

    rng = np.random.default_rng(seed)
    hits = read_matrix(matrix_path)
    test = [q for q in load_questions(split="test") if not q.is_trap]
    learn_ids = [q.id for q in load_questions(split="learn")]
    engines = list(ENGINES)
    subs = subsets(test)
    arrays = {name: hit_array(hits, ids, engines) for name, ids in subs.items()}
    if arrays["overall"].shape[0] == 0:
        raise MatrixIncomplete(f"no test question has a row for every engine in {matrix_path}")
    rates = {name: rate_table(arr, engines, rng, n_boot) for name, arr in arrays.items()}
    primary = {e: PRICE_USD.get(e, DEFAULT_PRICE_USD) for e in engines}
    price_tables = {"so.config.PRICE_USD (what Telem bills us)": primary,
                    "Telem latency-study list prices": {e: LIST_PRICE_USD.get(e) for e in engines}}
    claims = judge_claims(arrays, engines, price_tables, rng, n_boot=n_boot)
    rpd = {name: rescues_per_dollar(hits, learn_ids, table, FIRST_ENGINE, engines)
           for name, table in price_tables.items()}
    table_order = json.loads(rescue_table_path.read_text())["order_after"][FIRST_ENGINE] \
        if rescue_table_path.exists() else []
    primary_order = next(iter(rpd.values()))["order"]
    differs = primary_order != table_order
    why = ""
    if differs and table_order:
        moved = [e for i, e in enumerate(primary_order) if i >= len(table_order) or table_order[i] != e]
        why = f"per dollar it is {', '.join(primary_order)}; positions change for {', '.join(moved)}"
    years = [int(y) for q in test for y in re.findall(r"(?<!\d)(?:19|20)\d\d(?!\d)", q.question + " " + " ".join(q.gold))]
    results = json.loads(results_path.read_text()) if results_path.exists() else None
    frontier = json.loads(frontier_path.read_text()) if frontier_path.exists() else None
    return {
        "split": "test", "non_trap_only": True, "n_test": int(arrays["overall"].shape[0]),
        "engines": engines, "seed": seed, "n_boot": n_boot,
        "subset_sizes": {k: int(v.shape[0]) for k, v in arrays.items()},
        "newest_year_mentioned": max(years) if years else None,
        "rates": rates, "claims": claims,
        "prices": {k: v for k, v in price_tables.items()},
        "rescues_per_dollar": rpd,
        "order_comparison": {"rescue_table": table_order, "per_dollar_primary": primary_order,
                             "differs": differs, "why": why},
        "oracle": oracle_capture(arrays["overall"], results, frontier),
        "context_telem_published": TELEM_CONTEXT,
        "recommendation_only": True,
    }


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point: writes out/hn_claims.json and out/hn_claims.md."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--matrix", type=Path, default=OUT_DIR / "matrix.jsonl")
    p.add_argument("--results", type=Path, default=OUT_DIR / "results.json")
    p.add_argument("--frontier", type=Path, default=OUT_DIR / "frontier.json")
    p.add_argument("--rescue-table", type=Path, default=OUT_DIR / "rescue_table.json")
    p.add_argument("--out-json", type=Path, default=OUT_JSON)
    p.add_argument("--out-md", type=Path, default=OUT_MD)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    doc = build(a.matrix, a.results, a.frontier, a.rescue_table)
    a.out_json.parent.mkdir(parents=True, exist_ok=True)
    a.out_json.write_text(json.dumps(doc, indent=2) + "\n")
    md = render_md(doc)
    a.out_md.write_text(md)
    logger.info("%s\nwrote %s and %s", md, a.out_json, a.out_md)


if __name__ == "__main__":
    main()
