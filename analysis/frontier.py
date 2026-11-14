"""Coverage vs cost frontier. Test split, non-trap questions.

Coverage = share of questions where the answer is in the top-5 results of at
least one engine used. It is NOT final answer accuracy.

Second Opinion point (preference order):
  1. Measured: out/results.json setups.second_opinion exists. coverage = its
     accuracy (coverage is not defined for the full agent, so the point is labelled
     "final accuracy"); x = cost_per_q_search + cost_per_q_checker. If
     cost_per_q_checker is missing the note says "checker cost missing".
  2. Simulated from the matrix: ask FIRST_ENGINE, on a miss try the first
     --max-rescues engines of the learn-built order_after[FIRST_ENGINE]. Checker
     error: if results.json has checker miss_recall and false_alarm_rate they are
     applied as expected values (a missed miss stops the chain with no answer; a
     false alarm costs one more search); otherwise the checker is perfect, and the
     note and chart label say so. Checker cost per check is derived, not guessed:
     tokens_in = len(checker prompt + query + goal + the 5 formatted results of
     that question and engine from matrix_results.jsonl) / --chars-per-token,
     tokens_out = --checker-tokens-out, priced with LLM_PRICE_USD[CHECKER_MODEL].

  python -m analysis.frontier
"""
from __future__ import annotations

import argparse
import itertools
import json
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from analysis import common  # noqa: E402
from analysis.common import (ENGINES, FIRST_ENGINE, load_rescue_table, need_matrix, price, read_jsonl,  # noqa: E402
                             split_ids)
from data.load import load_all  # noqa: E402
from so.checker import load_prompt, render_user  # noqa: E402
from so.config import CHECKER_MODEL, LLM_PRICE_USD  # noqa: E402
from so.contracts import Frontier, Result  # noqa: E402

DEFAULT_MAX_RESCUES = 2  # Up to two rescues
DEFAULT_CHECKER_TOKENS_OUT = 150  # the checker replies with one short JSON object
DEFAULT_CHARS_PER_TOKEN = 4  # common rule of thumb for English text


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


def read_results_json() -> dict | None:
    f = common.OUT / "results.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:
        return None


def checker_costs(ids: set[str], tokens_out: int, chars_per_token: float) -> tuple[dict, float]:
    """(question_id, engine) -> derived USD cost of one checker call; plus the mean."""
    rates = LLM_PRICE_USD[CHECKER_MODEL]
    # so.config stores {"input": .., "output": ..} per model (USD per million tokens).
    p_in, p_out = (rates["input"], rates["output"]) if isinstance(rates, dict) else rates
    system = load_prompt("checker")
    text = {q.id: q.question for q in load_all() if q.id in ids}
    costs: dict[tuple[str, str], float] = {}
    for r in read_jsonl(common.OUT / "matrix_results.jsonl"):
        qid, eng = r["question_id"], r["engine"]
        if qid not in text or eng == "telem_default":
            continue
        results = [Result.model_validate(x) for x in (r.get("results") or [])]
        user = render_user(text[qid], text[qid], results, None)  # matrix uses question as query and goal
        tokens_in = (len(system) + len(user)) / chars_per_token
        costs[(qid, eng)] = (tokens_in * p_in + tokens_out * p_out) / 1e6
    if not costs:
        raise SystemExit(f"no usable rows in {common.OUT / 'matrix_results.jsonl'} to derive the checker cost")
    return costs, sum(costs.values()) / len(costs)


def simulate_so(pq: dict, order: list[str], max_rescues: int, chk: dict, chk_mean: float,
                recall: float | None, false_alarm: float | None) -> dict:
    """Expected coverage and cost per question. recall/false_alarm None means a perfect checker."""
    perfect = recall is None or false_alarm is None
    rec, far = (1.0, 0.0) if perfect else (recall, false_alarm)
    stages = [FIRST_ENGINE] + order[:max_rescues]
    cov = cost = 0.0
    n = 0
    for q, d in pq.items():
        if FIRST_ENGINE not in d:
            continue
        n += 1
        states = {False: 1.0, True: 0.0}  # already holding a hit -> probability mass still searching
        for i, e in enumerate(stages):
            if e not in d:
                break
            hit, c = d[e]
            nxt = {False: 0.0, True: 0.0}
            for found, mass in states.items():
                if not mass:
                    continue
                cost += mass * (c + chk.get((q, e), chk_mean))
                now = found or hit
                if i == len(stages) - 1:
                    cov += mass * now
                    continue
                go_on = far if hit else rec  # checker says "not a hit" -> rescue
                cov += mass * (1 - go_on) * now
                nxt[now] += mass * go_on
            states = nxt
    if perfect:
        how = "perfect checker (no results.json checker metrics yet)"
    else:
        how = f"checker error rates from results.json (miss_recall {rec:.3f}, false_alarm_rate {far:.3f}) applied as expected values"
    return {"kind": "second_opinion", "engines": [], "coverage": round(cov / n, 4),
            "cost_per_q": round(cost / n, 6), "n_questions": n, "simulated": True,
            "label": "simulated, " + ("perfect checker" if perfect else "measured checker error"),
            "rescue_order": stages[1:], "checker_cost_usd_mean": round(chk_mean, 7),
            "note": f"simulated from the matrix: {FIRST_ENGINE} first, up to {max_rescues} rescues in learned order, {how}. "
                    f"Checker cost per check derived from prompt length / token ratio and LLM_PRICE_USD[{CHECKER_MODEL}], "
                    "averaged over the checks made."}


def measured_runs(res: dict | None) -> dict | None:
    """Retrieval coverage of each setup's agent runs from out/results.json, or None if absent.

    Same metric as the combo points ("answer found in results", judged offline on the
    exact lists the agents saw) on one cohort of test questions. Refuses a Second
    Opinion point whose checker cost is unknown: a savings claim must count it.
    """
    rt = (res or {}).get("retrieval") or {}
    setups = rt.get("setups") or {}
    so = setups.get("second_opinion")
    if not so or so.get("coverage") is None or not rt.get("cohort_ids"):
        return None
    if so.get("cost_per_q_checker") is None:
        raise SystemExit("out/results.json has no checker cost for second_opinion; refusing to plot a savings point")
    points = {}
    for setup, v in setups.items():
        if v.get("coverage") is None:
            continue
        points[setup] = {"kind": setup, "engines": [], "coverage": float(v["coverage"]),
                         "cost_per_q": round(float(v["cost_per_q_search"]) + float(v.get("cost_per_q_checker") or 0), 6),
                         "n_questions": int(v["n_questions"]), "simulated": False,
                         "label": "measured agent runs",
                         "note": "agent runs; x = search + checker cost per question; " + rt.get("metric", "")}
    return {"cohort_ids": set(rt["cohort_ids"]), "points": points}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-rescues", type=int, default=DEFAULT_MAX_RESCUES)
    ap.add_argument("--checker-tokens-out", type=int, default=DEFAULT_CHECKER_TOKENS_OUT)
    ap.add_argument("--chars-per-token", type=float, default=DEFAULT_CHARS_PER_TOKEN)
    args = ap.parse_args(argv)
    ids = split_ids("test")
    res = read_results_json()
    measured = measured_runs(res)
    if measured:  # one cohort for every point: the questions all three setups finished
        ids = ids & measured["cohort_ids"]
    pq = per_question(need_matrix(), ids)
    combos = combo_points(pq)
    front = efficient(combos)

    drows = per_question([r for r in read_jsonl(common.OUT / "default_matrix.jsonl") if r["engine"] == "telem_default"], ids)
    nd = len(drows)
    if not nd:
        raise SystemExit(f"no default rows in {common.OUT / 'default_matrix.jsonl'}")
    default = {"kind": "telem_default", "engines": [],
               "coverage": round(sum(h for h, _ in (d["telem_default"] for d in drows.values())) / nd, 4),
               "cost_per_q": round(sum(c for _, c in (d["telem_default"] for d in drows.values())) / nd, 6),
               "n_questions": nd}

    so_pt = measured["points"].get("second_opinion") if measured else None
    if measured and "telem_default" in measured["points"]:
        default = measured["points"]["telem_default"]  # agent runs, same metric and cohort as Second Opinion
    if so_pt is None:
        chk, chk_mean = checker_costs(ids, args.checker_tokens_out, args.chars_per_token)
        m = (res or {}).get("checker") or {}
        recall, far = m.get("miss_recall"), m.get("false_alarm_rate")
        if not m.get("n_searches"):  # metrics block present but empty
            recall = far = None
        so_pt = simulate_so(pq, load_rescue_table()["order_after"][FIRST_ENGINE], args.max_rescues,
                            chk, chk_mean, recall, far)

    points = combos + [default, so_pt]
    doc = {"split": "test", "non_trap_only": True, "generated_from": "real data",
           "frontier_engines": [p["engines"] for p in front], "points": points,
           "measured_runs": list(measured["points"].values()) if measured else [],
           "combo_points_are": "one search per engine on the question text (hit matrix)"}
    Frontier.model_validate(doc)  # frontier contract
    common.OUT.mkdir(parents=True, exist_ok=True)
    (common.OUT / "frontier.json").write_text(json.dumps(doc, indent=2))
    plot(combos, front, default, so_pt)

    n_combo = combos[0]["n_questions"]
    print(f"{len(combos)} combos, each over {n_combo} test questions (non-trap)")
    for p in front:
        print(f"  frontier {'+'.join(p['engines']):45} cov {p['coverage']:.1%} cost ${p['cost_per_q']:.4f}")
    for p in (default, so_pt):
        sim = " (simulated)" if p.get("simulated") else ""
        print(f"  {p['kind']}{sim}: cov {p['coverage']:.1%} cost ${p['cost_per_q']:.4f} n={p['n_questions']}")
    print(f"wrote {common.OUT / 'frontier.json'} and .png")


def plot(combos, front, default, so_pt) -> None:
    fig, ax = plt.subplots(figsize=(13.33, 7.5), dpi=200)
    ax.scatter([p["cost_per_q"] for p in combos], [p["coverage"] for p in combos], s=45,
               color="#b9c6d6", alpha=0.8, zorder=2, label="engine combinations (63)")
    ax.plot([p["cost_per_q"] for p in front], [p["coverage"] for p in front], color="#1f3b63",
            lw=3, marker="o", ms=6, zorder=3, label="efficient frontier")
    ax.scatter([default["cost_per_q"]], [default["coverage"]], s=260, color="#7a7a7a", zorder=5,
               edgecolor="black")
    dlab = "Telem default" + ("\n(measured agent runs)" if default.get("label") == "measured agent runs" else "")
    ax.annotate(dlab, (default["cost_per_q"], default["coverage"]), xytext=(14, -26),
                textcoords="offset points", fontsize=20, color="#444444", fontweight="bold")
    ax.scatter([so_pt["cost_per_q"]], [so_pt["coverage"]], s=320, color="#e8590c", zorder=6,
               edgecolor="black")
    lab = f"Second Opinion\n({so_pt['label']})"
    ax.annotate(lab, (so_pt["cost_per_q"], so_pt["coverage"]), xytext=(30, -120), ha="left",
                arrowprops={"arrowstyle": "-", "color": "#c2410c", "lw": 2},
                textcoords="offset points", fontsize=20, color="#c2410c", fontweight="bold")
    ax.set_xlabel("search cost per question (search + checker)", fontsize=22)
    ax.set_ylabel("answer found in results", fontsize=22)
    ax.xaxis.set_major_formatter(lambda x, _: f"${x:.3f}")
    ax.yaxis.set_major_formatter(lambda y, _: f"{y:.0%}")
    ax.tick_params(labelsize=17)
    ax.grid(alpha=0.25)
    ax.legend(loc="lower right", fontsize=16, frameon=False)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ttl = f"Coverage vs cost, test split ({combos[0]['n_questions']} questions)"
    ax.set_title(ttl, fontsize=22, loc="left")
    fig.tight_layout()
    fig.savefig(common.OUT / "frontier.png")


if __name__ == "__main__":
    main()
