# Conformal bound on wrong HITs

**Test numbers below come from the 20-question DRY RUN, not the full eval. Rerun after the eval finishes.**

Slide line: Calibrated on the learn split to keep wrong HITs at or below 10%, checker HITs at confidence 0.80 or higher were wrong 16.7% of the time on 18 held-out searches (95% CI 3.6% to 41.4%), covering 58.1% of first-engine searches; that is above the 10% target.

Leakage check: 100 learn question ids, 20 test question ids, overlap 0. Test logs: DRY RUN logs (full eval logs not available yet) (20 finished second_opinion runs).

| alpha | lambda | learn HITs kept | learn wrong | learn ratio | test HITs kept | test wrong | test wrong-HIT rate (95% CI) | coverage | HIT retention |
|---|---|---|---|---|---|---|---|---|---|
| 0.10 | 0.80 | 67 | 4 | 0.074 | 18 | 3 | 16.7% (3.6% to 41.4%) | 58.1% | 100.0% |
| 0.05 | 0.93 | 36 | 0 | 0.027 | 6 | 1 | 16.7% (0.4% to 64.1%) | 19.4% | 33.3% |
| 0.15 | 0.80 | 67 | 4 | 0.074 | 18 | 3 | 16.7% (3.6% to 41.4%) | 58.1% | 100.0% |

Learn-split ratio (wrong + 1) / (HITs + 1) at each candidate lambda: 0.80: 0.073 (4/67), 0.85: 0.076 (4/65), 0.88: 0.061 (3/64), 0.90: 0.062 (3/63), 0.93: 0.027 (0/36), 0.95: 0.062 (0/15), 0.96: 0.167 (0/5).

Calibration: 100 learn searches on tavily, 67 HITs, 4 wrong. The checker already turns HITs below its own minimum confidence into SHALLOW, so every HIT here clears that floor.

Caveats: the test truth is the matrix row for (question, first engine), which used the question text as the query; the agent's query differs, so the join is approximate (as in evals/score.py). Calibration checks use the question as query and goal while test checks use the agent's, so exchangeability is approximate. Coverage is kept HITs over all first-engine checks; HIT retention is kept HITs over all HITs.
