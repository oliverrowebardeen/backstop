"""Run the fifteen hand-made cases through the real checker model.

    python -m tests.o1.live_examples

Prints each case's expected and actual verdict and the agreement. Costs one
CHECKER_MODEL call per case; repeat runs are served from the llm cache.
"""
from __future__ import annotations

import asyncio

from so.checker import check
from tests.o1.examples import EXAMPLES, TODAY


async def main() -> None:
    outs = await asyncio.gather(*(check(e["query"], e["goal"], e["results"], TODAY) for e in EXAMPLES))
    agree = hit_agree = 0
    for ex, out in zip(EXAMPLES, outs):
        ok = out.verdict == ex["expected"]
        agree += ok
        hit_agree += (out.verdict == "HIT") == (ex["expected"] == "HIT")
        mark = "ok " if ok else "XX "
        print(f"{mark}{ex['id']:22} expected {ex['expected']:12} got {out.verdict:12} "
              f"{out.confidence:.2f}  {out.why}")
    cost = sum(o.cost_usd for o in outs)
    print(f"\nverdict agreement {agree}/{len(EXAMPLES)}, HIT vs miss agreement {hit_agree}/{len(EXAMPLES)}, "
          f"cost ${cost:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
