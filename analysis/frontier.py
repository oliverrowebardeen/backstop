"""Coverage vs cost frontier (lane S3), section 6.13. Test split, non-trap questions.

Coverage = share of questions where the answer is in the top-5 results of at
least one engine used. It is NOT final answer accuracy.

Second Opinion point:
  * If out/results.json has setups.second_opinion, that point uses its measured
    accuracy and cost_per_q_search, labelled as final-answer accuracy
    (coverage is not defined there). simulated: false.
  * Otherwise it is simulated from the matrix (simulated: true): ask tavily; on a
    miss try the first MAX_RESCUES engines of order_after["tavily"] from the
    learn-built rescue table. The checker is assumed perfect (stops exactly on a
    hit), and every result list checked costs CHECKER_COST_USD.

  python -m analysis.frontier
"""
from __future__ import annotations

import itertools
import json
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from analysis.common import (ENGINES, FIRST_ENGINE, OUT, banner, load_rescue_table, need_matrix,  # noqa: E402
                             price, read_jsonl, split_ids)

CHECKER_COST_USD = 0.0006  # estimated cost of one checker call on one result list (small model, ~1.5k tokens in)
MAX_RESCUES = 2


def per_question(rows: list[dict], ids: set[str]) -> dict[str, dict[str, tuple[bool, float]]]:
    """question_id -> engine -> (hit, cost_usd). Missing cost falls back to PRICE_USD."""
    out: dict[str, dict[str, tuple[bool, float]]] = defaultdict(dict)
    for r in rows:
        if r["question_id"] in ids:
            c = r.get("cost_usd")
            out[r["question_id"]][r["engine"]] = (bool(r.get("hit")), float(c) if c is not None else price(r["engine"]))
    return dict(out)


def combo_points(pq: dict) -> list[dict]:
    complete = [q for q, d in pq.items() if all(e in d for e in ENGINES)]
    pts = []
    for k in range(1, len(ENGINES) + 1):
        for combo in itertools.combinations(ENGINES, k):
            cov = sum(any(pq[q][e][0] for e in combo) for q in complete) / len(complete)
            cost = sum(sum(pq[q][e][1] for e in combo) for q in complete) / len(complete)
            pts.append({"kind": "combo", "engines": list(combo), "coverage": round(cov, 4),
                        "cost_per_q": round(cost, 6), "n_questions": len(complete)})
    return pts


def efficient(points: list[dict]) -> list[dict]:
    best, out = -1.0, []
    for p in sorted(points, key=lambda p: (p["cost_per_q"], -p["coverage"])):
        if p["coverage"] > best:
            out.append(p)
            best = p["coverage"]
    return out


def simulate_so(pq: dict, order: list[str]) -> dict:
    rescues = order[:MAX_RESCUES]
    cov = cost = 0.0
    n = 0
    for q, d in pq.items():
        if FIRST_ENGINE not in d:
            continue
        n += 1
        found = False
        for e in [FIRST_ENGINE] + rescues:
            if e not in d:
                continue
            cost += d[e][1] + CHECKER_COST_USD
            if d[e][0]:
                found = True
                break
        cov += found
    return {"kind": "second_opinion", "engines": [], "coverage": round(cov / n, 4),
            "cost_per_q": round(cost / n, 6), "n_questions": n, "simulated": True,
            "rescue_order": rescues, "checker_cost_usd": CHECKER_COST_USD,
            "note": "simulated from the matrix: perfect checker, first engine tavily, up to "
                    f"{MAX_RESCUES} rescues in learned order; real checker errors would lower coverage"}


def measured_so() -> dict | None:
    f = OUT / "results.json"
    if not f.exists():
        return None
    try:
        so = json.loads(f.read_text())["setups"]["second_opinion"]
        return {"kind": "second_opinion", "engines": [], "coverage": float(so["accuracy"]),
                "cost_per_q": float(so["cost_per_q_search"]), "n_questions": None, "simulated": False,
                "coverage_is": "final answer accuracy (coverage is not defined for the full agent)",
                "note": "measured in out/results.json; x is search cost per question, checker cost not included"}
    except (KeyError, ValueError, TypeError):
        return None


def main() -> None:
    ids = split_ids("test")
    pq = per_question(need_matrix(), ids)
    combos = combo_points(pq)
    front = efficient(combos)

    drows = per_question([r for r in read_jsonl(OUT / "default_matrix.jsonl") if r["engine"] == "telem_default"], ids)
    nd = len(drows)
    if not nd:
        raise SystemExit(f"no default rows in {OUT / 'default_matrix.jsonl'}")
    default = {"kind": "telem_default", "engines": [],
               "coverage": round(sum(h for h, _ in (d["telem_default"] for d in drows.values())) / nd, 4),
               "cost_per_q": round(sum(c for _, c in (d["telem_default"] for d in drows.values())) / nd, 6),
               "n_questions": nd}

    so_pt = measured_so()
    if so_pt is None:
        so_pt = simulate_so(pq, load_rescue_table()["order_after"][FIRST_ENGINE])

    points = combos + [default, so_pt]
    doc = {"split": "test", "non_trap_only": True, "generated_from": banner(),
           "frontier_engines": [p["engines"] for p in front], "points": points}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "frontier.json").write_text(json.dumps(doc, indent=2))
    plot(combos, front, default, so_pt)

    n_combo = combos[0]["n_questions"]
    print(f"{len(combos)} combos, each over {n_combo} test questions (non-trap)")
    for p in front:
        print(f"  frontier {'+'.join(p['engines']):45} cov {p['coverage']:.1%} cost ${p['cost_per_q']:.4f}")
    for p in (default, so_pt):
        sim = " (simulated)" if p.get("simulated") else ""
        print(f"  {p['kind']}{sim}: cov {p['coverage']:.1%} cost ${p['cost_per_q']:.4f} n={p['n_questions']}")
    print(f"wrote {OUT / 'frontier.json'} and .png")


def plot(combos, front, default, so_pt) -> None:
    fig, ax = plt.subplots(figsize=(13.33, 7.5), dpi=200)
    ax.scatter([p["cost_per_q"] for p in combos], [p["coverage"] for p in combos], s=45,
               color="#b9c6d6", alpha=0.8, zorder=2, label="engine combinations (63)")
    ax.plot([p["cost_per_q"] for p in front], [p["coverage"] for p in front], color="#1f3b63",
            lw=3, marker="o", ms=6, zorder=3, label="efficient frontier")
    ax.scatter([default["cost_per_q"]], [default["coverage"]], s=260, color="#7a7a7a", zorder=5,
               edgecolor="black")
    ax.annotate("Telem default", (default["cost_per_q"], default["coverage"]), xytext=(14, -26),
                textcoords="offset points", fontsize=20, color="#444444", fontweight="bold")
    ax.scatter([so_pt["cost_per_q"]], [so_pt["coverage"]], s=320, color="#e8590c", zorder=6,
               edgecolor="black")
    lab = "Second Opinion" + ("\n(simulated)" if so_pt.get("simulated") else "\n(final accuracy)")
    ax.annotate(lab, (so_pt["cost_per_q"], so_pt["coverage"]), xytext=(30, -120), ha="left",
                arrowprops={"arrowstyle": "-", "color": "#c2410c", "lw": 2},
                textcoords="offset points", fontsize=20, color="#c2410c", fontweight="bold")
    ax.set_xlabel("search cost per question", fontsize=22)
    ax.set_ylabel("answer found in results", fontsize=22)
    ax.xaxis.set_major_formatter(lambda x, _: f"${x:.3f}")
    ax.yaxis.set_major_formatter(lambda y, _: f"{y:.0%}")
    ax.tick_params(labelsize=17)
    ax.grid(alpha=0.25)
    ax.legend(loc="lower right", fontsize=16, frameon=False)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ttl = f"Coverage vs cost, test split ({combos[0]['n_questions']} questions)"
    if banner().startswith("FAKE"):
        ttl += "  [FAKE DATA]"
    ax.set_title(ttl, fontsize=22, loc="left")
    fig.tight_layout()
    fig.savefig(OUT / "frontier.png")


if __name__ == "__main__":
    main()
