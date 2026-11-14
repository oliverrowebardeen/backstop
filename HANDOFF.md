
## S1: Questions and traps
- Built: data/questions.jsonl (300: 235 fact from SimpleQA, 65 finance from FinanceBench; 100 learn / 200 test, stratified, seed 14) and data/traps.jsonl (30 sourced false-premise questions, all test). Loader in data/load.py: load_questions(split, category), load_traps(), load_all(split).
- Run: python -m data.build_questions (rebuild), python -m data.load --stats (check).
- Not done: FinanceBench only had 65 questions with short checkable golds, so 5 extra SimpleQA fill the 70 slot. Traps need Shikhar's 11:30 read-through.

## S3: Rescue table, frontier, heatmap
- Built: analysis/{rescue_table,frontier,heatmap,findings}.py (run `python -m analysis.<name>`, order: rescue_table, frontier, heatmap, findings). Rescue table is learn-only and raises on test or trap rows. Second Opinion point is simulated (perfect checker, 2 rescues, CHECKER_COST_USD 0.0006) until out/results.json exists.
- Tested on fake data only (SO_FAKE=1, outputs in out/fake/); the real-data rerun is pending the real out/matrix.jsonl, then rerun all four and commit out/rescue_table.json, frontier and heatmap files. analysis/findings.md currently carries a FAKE DATA banner and must be regenerated.
