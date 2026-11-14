
## S1: Questions and traps
- Built: data/questions.jsonl (300: 235 fact from SimpleQA, 65 finance from FinanceBench; 100 learn / 200 test, stratified, seed 14) and data/traps.jsonl (30 sourced false-premise questions, all test). Loader in data/load.py: load_questions(split, category), load_traps(), load_all(split).
- Run: python -m data.build_questions (rebuild), python -m data.load --stats (check).
- Not done: FinanceBench only had 65 questions with short checkable golds, so 5 extra SimpleQA fill the 70 slot. Traps need Shikhar's 11:30 read-through.

## S3: Rescue table, frontier, heatmap
- Built: analysis/{rescue_table,frontier,heatmap,findings}.py (run `python -m analysis.<name>`, order: rescue_table, frontier, heatmap, findings). Rescue table is learn-only and raises on test or trap rows. Second Opinion point is simulated (perfect checker, 2 rescues, CHECKER_COST_USD 0.0006) until out/results.json exists.
- Tested on fake data only (SO_FAKE=1, outputs in out/fake/); the real-data rerun is pending the real out/matrix.jsonl, then rerun all four and commit out/rescue_table.json, frontier and heatmap files. analysis/findings.md currently carries a FAKE DATA banner and must be regenerated.

## S4: Eval runner and scoring
- Built: evals/run_eval.py (230 x 3 runs, resumable on run_end, LLM_MAX_CONCURRENCY, --limit/--setups/--traps-only/--max-test, writes out/eval_manifest.json) and evals/score.py (out/results.json in the 6.12 shape, validated against so.contracts.Results, plus cost_per_q_checker and searches_per_q, and out/results.md with counts, traps, checker join, paired flips; judge cost excluded). Logs read via so.eventlog; config and setups from so.config / so.contracts. Fake agent in tests/s4/, used only with SO_FAKE=1.
- Run: python -m evals.run_eval --limit 20 && python -m evals.score (full: drop --limit; at 2:30 if short on time: --max-test 120). Fake check: SO_FAKE=1 python -m evals.matrix, then the same two commands. Tests: python -m pytest tests/s4.
- Not done: real run pending so.agent.run_question (the one switch is load_agent() in evals/run_eval.py). After the real run, re-run python -m analysis.frontier so the measured Second Opinion point replaces the simulated one.

- O3 built: shared agent loop, setup adapters, session propagation, cited answers, bounded tools, and complete cost totals. The shared prompt requires an answer after HIT unless a separate question part remains; exact repeat queries reuse their prior outcome within a run without another search or duplicate retrieval costs.
- O3 run: `.venv/bin/pytest tests/o3 -q` passes 32 tests. `.venv/bin/python -m so.agent --setup <setup> --question "Who is the CEO of Tavily?"` returns ANSWERED with citations for all three setups: single uses 1 search, 1 engine call, $0.021450 total; telem_default uses 4 searches, 16 engine calls, 2 fetches, $0.522066 total; second_opinion uses 1 search, 1 engine call, $0.0167747 total.
- O3 not done: none. The real Second Opinion run answered immediately after its first HIT. Totals match eventlog.summarize and include model/fetch costs and original costs for cached calls.

- O0 built: shared contracts, environment configuration, cached/budgeted Telem and Tavily wrappers, Anthropic/OpenAI model wrapper, and JSONL event log. `client.providers()` confirmed tavily, exa, brave, perplexity, parallel, ceramic; SDK/docs confirm `client.fetch(urls, session=...)` accepts no goal, while Tavily Extract forwards goal as query.
- O0 run: `uv venv --python 3.11 && uv pip install -e '.[dev]'`; `.venv/bin/python -m pytest tests/o0 -q` passed 28 tests; O0 plus O1 passed 117. Live Haiku structured output, Sonnet tool calls, and Tavily Extract passed. Acceptance: `.venv/bin/python -m so.telem_client "who founded Tavily" --engines tavily exa`, then repeat to verify cached results.
- O0 not done: Telem live acceptance and its cached rerun await a valid TELEM_API_KEY; the shared key returns HTTP 401. Merged to main and lane/o0-foundation on green unit tests as authorized. Capture the first live EngineRun.session_id in RunContext.session and forward it on subsequent Telem calls; agent/rescue settings and SO_LOG_DIR (LOG_DIR alias) are ready.

- O2 built: configurable rescue policy, per-run retrieval memory, and Second Opinion search with URL deduplication, checker usage totals, goal propagation, and Tavily Extract with Telem fetch fallback.
- O2 run: `.venv/bin/python -m pytest tests/o2 -q` passes 55 tests; `.venv/bin/python -m so.second_opinion "What was Stripe's valuation in its 2023 round?" --goal "find the valuation figure"` returns HIT with $50 billion evidence from Stripe's newsroom, Tavily then Exa, 2 engine calls, 0 fetches, $0.0120596 including checker usage, and 11403.02 ms. Cached rerun returns the same result, cost and original latency with 2/2 searches cached.
- O2 not done: O1 should investigate truncated checker JSON on the Tavily results (399 output tokens per attempt); its retry also failed to parse, then the Exa rescue succeeded. O2 records the failure and all checker usage. Live and cached acceptance now pass with the corrected Telem event endpoint.

## S2: Hit matrix and judge
- Built: evals/judge.py (judge_hit, grade_answer on JUDGE_MODEL via so.llm, disk-cached), evals/matrix.py (330 questions x 6 engines + Telem default, async, resumable, --rejudge), evals/contamination.py (drops benchmark copies before judging), evals/handcheck.py. Outputs on main: out/matrix.jsonl (1,979 rows), out/default_matrix.jsonl (330), out/matrix_results.jsonl (raw top-5 per row, what the judge saw before filtering).
- Run: python -m evals.matrix [--limit N] | --summary | --rejudge ; python -m evals.handcheck. Live cost: $27.17 collection + $2.35 re-judge. One ceramic row (1 test question) still errors with a 503.
- Hit rates (after the contamination filter; traps = results contain evidence the premise is false):

| engine | learn | test | traps | test, before filter |
|---|---|---|---|---|
| tavily | 70.0% (70/100) | 68.5% (137/200) | 90.0% (27/30) | 70.5% (141/200) |
| exa | 80.0% (80/100) | 82.0% (164/200) | 90.0% (27/30) | 84.0% (168/200) |
| brave | 70.0% (70/100) | 67.0% (134/200) | 86.7% (26/30) | 69.5% (139/200) |
| perplexity | 78.0% (78/100) | 82.0% (164/200) | 80.0% (24/30) | 96.5% (193/200) |
| parallel | 74.0% (74/100) | 63.5% (127/200) | 83.3% (25/30) | 67.5% (135/200) |
| ceramic | 2.0% (2/100) | 4.5% (9/199) | 33.3% (10/30) | 5.5% (11/199) |
| telem_default | 87.0% (87/100) | 90.0% (180/200) | 100.0% (30/30) | 97.0% (194/200) |

- Contamination: 667 engine results and 501 Telem-default results were copies of the benchmark (Hugging Face viewers, published FinanceBench answer sheets, pages quoting the question verbatim). Perplexity was hit hardest (96.5% to 82.0% on test). Oliver: please apply evals.contamination.filter_results in so/checker_eval.py before checking (otherwise the checker can say HIT on a copy the truth calls a miss), and consider it in the agent's search path for the eval.
- Telem endpoint: the event's keys come from hack.telem.ai and its router is https://hack.telem.ai/api/router (set TELEM_BASE_URL; the SDK reads it). On that deployment Telem's default routing chose 7 engines for a test query, not 3, so the telem_default baseline costs more there. The matrix above was collected on router.telem.ai before the switch.
- Judge check: out/judge_agreement.json = 29/30 agree (97%), 15 judge hits + 15 misses sampled with seed 14. Labeled blind by a model (claude-opus-5-5), NOT a human: do not present it as a human check. For a human number, move the file aside and run python -m evals.handcheck (about ten minutes). The one disagreement (sqa-2758, brave) is a truncated snippet the judge read leniently. Traps reviewed: all 30 are sourced to settled public facts (abandoned deals, founders, never-happened IPOs, misattributed products and HQ moves); none removed.

- O4 built: FastAPI race/replay streams and results API, race/results UI with Telem provenance, source links, light/dark themes, JSON audit exports, and environment-backed server settings; fixture loading is disabled by default.
- O4 run: `.venv/bin/pytest tests/ -q` passes 261 tests; `node --test tests/o4/test_ui.mjs` passes 9 tests. `SERVER_FIXTURE_DIR=tests/o4/fixtures python -m server` serves the 25-event replay with matching totals. Live `POST /race` for "Who is the CEO of Tavily?" returned 200 and completed both runs with 7/9 events, $0.015414/$0.0171677, and 3 citations each.
- O4 not done: none within lane scope. Run one server worker. Recordings use the manifest format in server/README.md; real evaluation artifacts are read from the configured output paths.
