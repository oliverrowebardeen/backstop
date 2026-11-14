
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

- O3 built: shared agent loop, baseline setup adapters, session propagation, citation validation, bounded tools, and event-based cost totals in so/agent.py, so/setups.py, and so/prompts/agent.md.
- O3 run: `.venv/bin/pytest tests/o3 -q` passes 25 tests; `.venv/bin/python -m so.agent --setup single --question "Who is the CEO of Tavily?"` and the same command with `--setup telem_default` are the live acceptance checks.
- O3 not done: live acceptance and main merge require a working shared Telem credential and O0 config exports AGENT_MAX_SEARCHES=6, AGENT_MAX_PAGE_READS=12, AGENT_MAX_TURNS=24, AGENT_PROMPT_PATH=so/prompts/agent.md, LOG_DIR=out/logs, and boolean SO_ENABLE_SECOND_OPINION. Both attempted live checks failed with Telem AuthError.
