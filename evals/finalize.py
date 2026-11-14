"""Turn finished eval logs into every reported artifact, in dependency order (lane S4).

    python -m evals.finalize            # score, then regenerate all analysis outputs
    python -m evals.finalize --dry-run  # print the steps without running them

Steps, each a separate process so one module's state cannot leak into the next:
  1. evals.score          -> out/results.json, out/results.md
  2. analysis.frontier    -> out/frontier.json/.png (measured Second Opinion point
                             replaces the simulated one once results.json exists)
  3. analysis.findings    -> analysis/findings.md
  4. analysis.conformal   -> out/conformal.json/.md (test side from the eval logs)
  5. analysis.hn_claims   -> out/hn_claims.json/.md (oracle share from results.json)

Stops at the first failing step and says which one failed. Nothing here makes a
search call; grading uses the judge cache, so a rerun on the same logs is free.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from evals.deps import OUT, ROOT

STEPS: list[tuple[str, list[str]]] = [
    ("score the eval", ["-m", "evals.score"]),
    ("frontier chart", ["-m", "analysis.frontier"]),
    ("findings", ["-m", "analysis.findings"]),
    ("conformal wrong-HIT bound", ["-m", "analysis.conformal", "--log-dir", str(OUT / "logs")]),
    ("founder claims", ["-m", "analysis.hn_claims", "--results", str(OUT / "results.json")]),
]
OUTPUTS = ["out/results.json", "out/results.md", "out/frontier.json", "out/frontier.png",
           "analysis/findings.md", "out/conformal.json", "out/conformal.md",
           "out/hn_claims.json", "out/hn_claims.md"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print the steps without running them")
    args = p.parse_args(argv)
    if not args.dry_run and not any((OUT / "logs").glob("*.jsonl")):
        print(f"no eval logs in {OUT / 'logs'}; run python -m evals.run_eval first", file=sys.stderr)
        return 2
    for i, (name, cmd) in enumerate(STEPS, 1):
        print(f"[{i}/{len(STEPS)}] {name}: python {' '.join(cmd)}", flush=True)
        if args.dry_run:
            continue
        done = subprocess.run([sys.executable, *cmd], cwd=ROOT)
        if done.returncode != 0:
            print(f"step {i} ({name}) failed with exit code {done.returncode}; later steps not run",
                  file=sys.stderr)
            return done.returncode
    if not args.dry_run:
        print("\nartifacts to commit (git add -f; out/ files are not ignored except logs):")
        for path in OUTPUTS:
            mark = "ok" if (ROOT / path).exists() else "MISSING"
            print(f"  {mark:7} {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
