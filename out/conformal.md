# Conformal bound on wrong HITs

Slide line: Calibrated on the learn split to keep wrong HITs at or below 10%, checker HITs at confidence 0.80 or higher were wrong 7.5% of the time on 174 held-out searches (95% CI 4.0% to 12.4%), covering 72.8% of first-engine searches.

Leakage check: 100 learn question ids, 198 test question ids, overlap 0. Test logs: logs (198 finished second_opinion runs).

| alpha | lambda | learn HITs kept | learn wrong | learn ratio | test HITs kept | test wrong | test wrong-HIT rate (95% CI) | coverage | HIT retention |
|---|---|---|---|---|---|---|---|---|---|
| 0.10 | 0.80 | 66 | 3 | 0.060 | 174 | 13 | 7.5% (4.0% to 12.4%) | 72.8% | 100.0% |
| 0.05 | 0.93 | 42 | 1 | 0.047 | 90 | 3 | 3.3% (0.7% to 9.4%) | 37.7% | 51.7% |
| 0.15 | 0.80 | 66 | 3 | 0.060 | 174 | 13 | 7.5% (4.0% to 12.4%) | 72.8% | 100.0% |

Learn-split ratio (wrong + 1) / (HITs + 1) at each candidate lambda: 0.80: 0.060 (3/66), 0.90: 0.062 (3/63), 0.93: 0.046 (1/42), 0.94: 0.059 (0/16), 0.95: 0.062 (0/15), 0.96: 0.250 (0/3), 0.97: 0.500 (0/1).

Calibration: 100 learn searches on tavily, 66 HITs, 3 wrong. The checker already turns HITs below its own minimum confidence into SHALLOW, so every HIT here clears that floor.

Test truth: an offline judgment of the exact (contamination-filtered) result list each first-engine check saw, recovered from the Telem response cache (evals/retrieval.py). Caveats: calibration checks use the question as query and goal while test checks use the agent's, so exchangeability is approximate. Coverage is kept HITs over all first-engine checks; HIT retention is kept HITs over all HITs.
