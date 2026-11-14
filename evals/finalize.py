"""Turn finished eval logs into the published results, frontier and findings.

    python -m evals.finalize            # score, then regenerate all analysis outputs
    python -m evals.finalize --dry-run  # print the steps without running them

Steps, each a separate process so one module's state cannot leak into the next:
  1. evals.score          -> out/results.json, out/results.md
  2. analysis.frontier    -> out/frontier.json/.png (measured Second Opinion point
                             replaces the simulated one once results.json exists)
  3. analysis.findings    -> analysis/findings.md

Stops at the first failing step and says which one failed. Nothing here makes a
search call; grading uses the judge cache, so a rerun on the same logs is free.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from evals import deps
from evals.deps import ROOT


def steps(so_log_dir: Path | None = None, so_label: str | None = None) -> list[tuple[str, list[str]]]:
    """Score the headline pass and its comparison, then regenerate the published analysis."""
    score = ["-m", "evals.score"]
    if so_log_dir is not None:
        score += ["--so-log-dir", str(so_log_dir)] + (["--so-label", so_label] if so_label else [])
    return [
        ("score the eval", score),
        ("frontier chart", ["-m", "analysis.frontier"]),
        ("findings", ["-m", "analysis.findings"]),
    ]


OUTPUTS = ["out/results.json", "out/results.md", "out/frontier.json", "analysis/findings.md"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print the steps without running them")
    p.add_argument("--so-log-dir", type=Path, default=None,
                   help="headline second_opinion from this extra pass (see evals.score --so-log-dir)")
    p.add_argument("--so-label", default=None, help="what the extra pass is (shown in results)")
    args = p.parse_args(argv)
    logs = deps.OUT / "logs"
    if not args.dry_run and not any(logs.glob("*.jsonl")):
        print(f"no eval logs in {logs}; run python -m evals.run_eval first", file=sys.stderr)
        return 2
    plan = steps(args.so_log_dir, args.so_label)
    for i, (name, cmd) in enumerate(plan, 1):
        print(f"[{i}/{len(plan)}] {name}: python {' '.join(cmd)}", flush=True)
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
