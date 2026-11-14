"""Rescue heatmap: engine-by-engine rescue rates, learn split.

  python -m analysis.heatmap
"""
from __future__ import annotations

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from analysis import common  # noqa: E402
from analysis.common import load_rescue_table  # noqa: E402


def main() -> None:
    t = load_rescue_table()
    engines = list(t["rescue"])
    n = len(engines)
    grid = np.full((n, n), np.nan)
    cells = []
    for i, a in enumerate(engines):
        for j, b in enumerate(engines):
            if a == b:
                continue
            c = t["rescue"][a][b]
            grid[i, j] = c["rate"]
            cells.append({"missed": a, "rescuer": b, **c})
    (common.OUT / "rescue_heatmap.json").write_text(json.dumps(
        {"split": "learn", "n_questions": t["n_questions"], "engines": engines, "cells": cells}, indent=2))

    fig, ax = plt.subplots(figsize=(13.33, 7.5), dpi=200)
    cmap = plt.get_cmap("Blues").copy()
    cmap.set_bad("#eeeeee")
    im = ax.imshow(np.ma.masked_invalid(grid), cmap=cmap, aspect="auto", vmin=0, vmax=max(0.5, np.nanmax(grid)))
    ax.set_xticks(range(n), engines, fontsize=17)
    ax.set_yticks(range(n), [f"{e}\n({t['miss_counts'][e]} misses)" for e in engines], fontsize=15)
    ax.xaxis.tick_top()
    ax.xaxis.set_label_position("top")
    ax.set_xlabel("engine tried next", fontsize=18, labelpad=12)
    ax.set_ylabel("engine that missed", fontsize=18)
    top = im.norm.vmax * 0.6
    for i, a in enumerate(engines):
        for j, b in enumerate(engines):
            if i == j:
                continue
            c = t["rescue"][a][b]
            col = "white" if grid[i, j] > top else "black"
            ax.text(j, i, f"{c['rescued']}/{c['misses']}", ha="center", va="center", fontsize=20,
                    fontweight="bold", color=col)
            ax.text(j, i + 0.3, f"{c['rate']:.0%}", ha="center", va="center", fontsize=13, color=col)
    cb = fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02)
    cb.set_label("share of misses rescued", fontsize=16)
    cb.ax.tick_params(labelsize=14)
    for s in ax.spines.values():
        s.set_visible(False)
    fig.suptitle(f"Which engine rescues which miss (learn split, {t['n_questions']} questions)",
                 fontsize=22, x=0.02, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(common.OUT / "rescue_heatmap.png")
    print(f"wrote {common.OUT / 'rescue_heatmap.json'} and .png")


if __name__ == "__main__":
    main()
