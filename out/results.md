# Second Opinion: eval results

Generated 2026-11-14T21:12:17+00:00. Test questions in scope: 200. Traps in scope: 30.
Every number comes from the test split or the traps. Judge cost is excluded from every setup.

**Headline second_opinion: CHECKER_BACKEND=cascade, CHECKER_CASCADE_THRESHOLD=0.7, run at commit 77b2045; baselines and the default-checker comparison from the main eval at commit edf0214.** Baselines: single and telem_default from logs. The main eval's second_opinion run (default checker) is reported below as `second_opinion_llm_checker`.

Unfinished runs: second_opinion scored 199 of 200. Re-run evals.run_eval to finish them.

## Test questions (traps excluded)

| | single | telem_default | second_opinion |
|---|---|---|---|
| Questions scored | 200 | 200 | 199 |
| Correct (accuracy) | 94.0% (188/200) | 94.5% (189/200) | 94.5% (188/199) |
| Wrong (wrong rate) | 4.0% (8/200) | 5.5% (11/200) | 5.0% (10/199) |
| Declined (NOT FOUND) | 2.0% (4/200) | 0.0% (0/200) | 0.5% (1/199) |
| Search tool calls per question | 1.86 (373/200) | 1.40 (280/200) | 1.22 (242/199) |
| Engine calls per question | 1.86 (373/200) | 8.00 (1600/200) | 1.31 (261/199) |
| Search cost per question | $0.0101 ($2.013/200) | $0.0321 ($6.417/200) | $0.0074 ($1.475/199) |
| Checker cost per question | $0.0000 ($0.000/200) | $0.0000 ($0.000/200) | $0.0005 ($0.108/199) |
| Total cost per question | $0.0632 ($12.637/200) | $0.1016 ($20.317/200) | $0.0478 ($9.519/199) |
| Cost per correct answer | $0.0672 ($12.637/188) | $0.1075 ($20.317/189) | $0.0506 ($9.519/188) |
| Cost per search call | $0.0050 ($1.865/373 calls) | $0.0225 ($6.295/280 calls) | $0.0061 ($1.477/242 calls) |
| Latency p50 | 9.0s (n=200) | 8.6s (n=200) | 7.3s (n=199) |
| Latency p95 | 32.6s (n=200) | 25.6s (n=200) | 24.8s (n=199) |

Cost per search call = (engine calls + page reads inside the search tool + checker model) divided by search tool calls. Total cost includes every agent and checker model call.

## Traps (false premise, reported separately)

| | single | telem_default | second_opinion |
|---|---|---|---|
| Traps scored | 30 | 30 | 30 |
| Confidently wrong | 0.0% (0/30) | 0.0% (0/30) | 0.0% (0/30) |
| Flagged the false premise | 93.3% (28/30) | 100.0% (30/30) | 100.0% (30/30) |
| Declined (NOT FOUND) | 6.7% (2/30) | 0.0% (0/30) | 0.0% (0/30) |

## Checker (second_opinion, every check vs the exact list it saw)

| Metric | Value |
|---|---|
| Checked searches judged | 261 (not in cache: 0, judge errors: 0) |
| Miss recall (true misses labelled not-HIT) | 75.6% (59/78) |
| False alarm rate (true hits labelled not-HIT) | 2.7% (5/183) |
| Rescuable vs DEAD accuracy (on true misses) | 5.1% (4/78) |

Truth: every check compared with an offline judgment of the exact result list it saw (recovered from the Telem response cache, contamination-filtered as in the eval). Traps are excluded.

## Retrieval coverage of the agent runs (common cohort: 198 test questions)

| Setup | Answer found in results | Search cost / q | Checker cost / q |
|---|---|---|---|
| single | 83.8% (166/198) | $0.0101 | $0.0000 |
| telem_default | 91.4% (181/198) | $0.0321 | $0.0000 |
| second_opinion | 81.3% (161/198) | $0.0074 | $0.0005 |

Metric: answer found in results: any search in the agent's run surfaced the gold answer (offline judge on the exact, contamination-filtered result lists).

## Paired comparison (test questions both setups finished)

| second_opinion vs | Both finished | Wrong to correct | Correct to wrong | Wrong to declined | Declined to correct | Correct to declined | Declined to wrong | Unchanged |
|---|---|---|---|---|---|---|---|---|
| single | 199 | 2 | 5 | 1 | 3 | 0 | 1 | 187 |
| telem_default | 199 | 4 | 4 | 1 | 0 | 0 | 0 | 190 |
| second_opinion_llm_checker | 198 | 3 | 1 | 1 | 1 | 0 | 0 | 192 |

Read each row as: the baseline's grade, then second_opinion's grade on the same question.

## Benchmark contamination filter

Search results that copy the benchmark (dataset viewers, published answer sheets, pages quoting the question) were removed before any setup, checker or agent saw them (evals/contamination.py, installed through so.telem_client.RESULT_FILTER).

| Setup | Results seen | Removed |
|---|---|---|
| single | 2105 | 1.5% (31/2105) |
| telem_default | 8704 | 2.2% (188/8704) |
| second_opinion | 1521 | 2.9% (44/1521) |

## Headline second_opinion vs second_opinion_llm_checker (same questions, same baselines)

| | second_opinion (headline) | second_opinion_llm_checker |
|---|---|---|
| Correct | 94.5% (188/199) | 93.4% (185/198) |
| Wrong | 5.0% (10/199) | 6.1% (12/198) |
| Declined | 0.5% (1/199) | 0.5% (1/198) |
| Total cost per question | $0.0478 | $0.0517 |
| Checker cost per question | $0.0005 | $0.0008 |
| Checker miss recall (exact lists) | 75.6% (59/78) | 83.8% (67/80) |
| Checker false alarms (exact lists) | 2.7% (5/183) | 8.7% (16/184) |

## Judge

- Hand check agreement: 96.7% (29/30), labeled by model:claude-opus-5-5 (blind, not human)
- Judge spend for grading this run: $0.0122 (1 calls, 2080 cached). Not included in any setup's cost.
