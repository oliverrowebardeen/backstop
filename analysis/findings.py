"""Write analysis/findings.md: three plain sentences, regenerable.

Learned order vs the fixed fallback order is compared on the TEST split
(non-trap), with a paired bootstrap over questions (--boot resamples, default 10,000; --seed, default 14) on
the difference in coverage. Policy for both: ask tavily first, then up to 2
rescue engines in that order; a question is covered if any of those engines hit.
"Beats by more than noise" means the --ci percent interval (default 95) of the difference
excludes zero.

  python -m analysis.findings
"""
from __future__ import annotations

import argparse

import numpy as np

from analysis.common import (FIXED_ORDER, FIRST_ENGINE, ROOT, hits_by_question, load_rescue_table, need_matrix,
                             split_ids)
from analysis.frontier import DEFAULT_MAX_RESCUES

# Where the findings are written (the committed file).
FINDINGS_MD = ROOT / "analysis" / "findings.md"
DEFAULT_BOOT = 10_000
DEFAULT_MIN_MISSES = 5
DEFAULT_SEED = 14  # the reproducible sampling seed
DEFAULT_CI = 95.0
DEFAULT_MIN_BASE = 0.25


def covered(h: dict[str, bool], order: list[str], max_rescues: int) -> bool:
    return any(h.get(e) for e in [FIRST_ENGINE] + order[:max_rescues])


def surprising_pair(t: dict, base: dict[str, float], min_misses: int, min_base: float = 0.0):
    """Largest lift = rescue rate minus the rescuing engine's overall hit rate (learn).

    Engines whose own hit rate is below `min_base` are skipped on both sides: an
    engine that misses almost everything makes any conditional rate trivially equal
    to the other engine's base rate, which says nothing about complementarity.
    """
    best = None
    for a, row in t["rescue"].items():
        if base.get(a, 0.0) < min_base:
            continue
        for b, c in row.items():
            if c["misses"] < min_misses or base.get(b, 0.0) < min_base:
                continue
            lift = c["rate"] - base[b]
            if best is None or lift > best[0]:
                best = (lift, a, b, c)
    return best


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", type=int, default=DEFAULT_BOOT)
    ap.add_argument("--min-misses", type=int, default=DEFAULT_MIN_MISSES)
    ap.add_argument("--min-base", type=float, default=DEFAULT_MIN_BASE,
                    help="skip engines below this learn hit rate when picking the surprising pair")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--ci", type=float, default=DEFAULT_CI, help="interval width in percent")
    ap.add_argument("--max-rescues", type=int, default=DEFAULT_MAX_RESCUES)
    args = ap.parse_args(argv)
    mr = args.max_rescues
    rows = need_matrix()
    t = load_rescue_table()
    learn = hits_by_question(rows, split_ids("learn"))
    base = {e: float(np.mean([h[e] for h in learn.values() if e in h])) for e in t["rescue"]}
    lift, a, b, c = surprising_pair(t, base, args.min_misses, args.min_base)

    test = hits_by_question(rows, split_ids("test"))
    test = {q: h for q, h in test.items() if all(e in h for e in t["rescue"])}
    qs = sorted(test)
    learned = t["order_after"][FIRST_ENGINE]
    yl = np.array([covered(test[q], learned, mr) for q in qs], float)
    yf = np.array([covered(test[q], FIXED_ORDER, mr) for q in qs], float)
    d = yl - yf
    rng = np.random.default_rng(args.seed)
    boots = rng.choice(d, size=(args.boot, len(d))).mean(axis=1)
    lo, hi = np.percentile(boots, [(100 - args.ci) / 2, 100 - (100 - args.ci) / 2])
    n_l, n_f = int((d > 0).sum()), int((d < 0).sum())
    verdict = ("beats the fixed order by more than noise" if lo > 0 else
               "is worse than the fixed order by more than noise" if hi < 0 else
               "does not beat the fixed order by more than noise")

    s1 = (f"On the learn split ({t['n_questions']} questions) {FIRST_ENGINE} missed {t['miss_counts'][FIRST_ENGINE]}, "
          f"and the best single rescue was {t['order_after'][FIRST_ENGINE][0]}, which found the answer in "
          f"{t['rescue'][FIRST_ENGINE][t['order_after'][FIRST_ENGINE][0]]['rescued']} of them.")
    pair = (f"{b} found the answer in {c['rescued']} of {c['misses']} {a} misses ({c['rate']:.0%}) "
            f"against its {base[b]:.0%} hit rate overall")
    if lift > 0:
        s2 = f"The most surprising pair is {a} then {b}: {pair}, {lift * 100:.0f} points above its average."
    else:
        s2 = (f"No engine rescues above its own average: the closest pair is {a} then {b}, where {pair}, "
              f"{abs(lift) * 100:.0f} points below. Misses are correlated, so a question one engine misses "
              f"is harder for every engine, which is why the rescue order has to be measured, not assumed.")
    s3 = (f"On the test split ({len(qs)} non-trap questions) the learned order ({', '.join(learned[:mr])}) "
          f"covers {yl.mean():.1%} against {yf.mean():.1%} for the fixed order ({', '.join(FIXED_ORDER[:mr])}), "
          f"a gap of {(yl.mean() - yf.mean()) * 100:+.1f} points (paired bootstrap {args.ci:g}% interval "
          f"{lo * 100:+.1f} to {hi * 100:+.1f}; {n_l} question{'s' if n_l != 1 else ''} favour{'s' if n_l == 1 else ''} learned, {n_f} favour{'s' if n_f == 1 else ''} fixed), "
          f"so the learned order {verdict}.")
    head = "> Generated by `python -m analysis.findings` from the real matrix.\n\n"
    text = head + " ".join([s1, s2, s3]) + "\n"
    FINDINGS_MD.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
