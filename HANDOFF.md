
## S1: Questions and traps
- Built: data/questions.jsonl (300: 235 fact from SimpleQA, 65 finance from FinanceBench; 100 learn / 200 test, stratified, seed 14) and data/traps.jsonl (30 sourced false-premise questions, all test). Loader in data/load.py: load_questions(split, category), load_traps(), load_all(split).
- Run: python -m data.build_questions (rebuild), python -m data.load --stats (check).
- Not done: FinanceBench only had 65 questions with short checkable golds, so 5 extra SimpleQA fill the 70 slot. Traps need Shikhar's 11:30 read-through.
