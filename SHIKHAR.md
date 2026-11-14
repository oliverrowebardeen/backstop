# Shikhar's lanes

Read `SHARED_CONTEXT.md` first. This file covers the questions, the hit matrix, the analysis, the eval and the pitch pack. Oliver's agents own `so/`, `server/`, `ui/` and `demo/`. Do not touch those.

All of these lanes run on Claude inside Paradev. Open one Paradev lane per row below, so each has its own workspace and branch.

## Lane map

| Lane | What | Window | Directories |
|---|---|---|---|
| S1 | Questions and traps | 11:00 to 11:40 | `data/` |
| S2 | Hit matrix and judge | 11:20 to 12:15 | `evals/matrix.py`, `evals/judge.py`, `evals/handcheck.py` |
| S3 | Rescue table, frontier, heatmap | 12:15 to 1:15 | `analysis/` |
| S4 | Eval runner and scoring | 12:30 to 3:15 | `evals/run_eval.py`, `evals/score.py` |
| S5 | Pitch pack | 2:30 to 4:10 | `pitch/` |

S2 and S4 are the heavy lanes. Give them your strongest model. S1, S3 and S5 can run on a lighter one.

## Kickoff message for each lane

Paste this, with the lane filled in:

```
Read SHARED_CONTEXT.md fully, then SHIKHAR.md. You are lane <ID>. Build only what
that lane lists, only in its directories. Import contracts from so/contracts.py.
If a dependency is not merged, use a fake in tests/<lane>/ with the same signatures.
Work on branch lane/<id>-<name>. When the acceptance check passes, merge straight
into main and push. Do not open a pull request. Then append your three lines to
HANDOFF.md and stop.
```

## S1: Questions and traps

Nothing blocks this lane. Start at 11:00.

Build:

1. `data/build_questions.py`: downloads public sets and writes `data/questions.jsonl` in the section 6.1 format.
   - 230 questions from SimpleQA (OpenAI's `simple-evals` test set), category `fact`.
   - 70 questions from FinanceBench (the open 150-question set from Patronus AI), category `finance`.
   - If a source cannot be reached, say so at once and fill from the other.
   - Keep only questions with a short, checkable gold answer. Drop any whose answer depends on today's date.
2. Split: 100 `learn`, 200 `test`, stratified by category, seed 14.
3. `data/traps.jsonl`: 30 false-premise questions about real, well-known companies, in a due-diligence style. Example shapes: a funding round that never happened, a founder attributed to the wrong company, an acquisition that was announced but never closed. Each needs a `trap_note` with a source URL showing the premise is false. All are `split: "test"`.
4. `data/load.py`: `load_questions(split=None, category=None)` and `load_traps()` returning `Question` objects.

Rules:

- Use companies and public executives only. No private individuals.
- Do not write a trap you cannot source.

Acceptance:

```
python -m data.load --stats
```

Prints 300 questions with the split and category counts, and 30 traps.

## S2: Hit matrix and judge

This is the most valuable data in the project. It feeds the checker test, the rescue table, the frontier chart and the heatmap.

Depends on `so/telem_client.py` and `so/llm.py`, due on `main` at 11:20. Until then, write against the signatures in sections 6.4 and 4 with a fake.

Build:

1. `evals/judge.py`:
   - `async def judge_hit(question, results) -> (hit, hit_rank, reason)`: sees the gold answer and the top-5 titles and snippets. Section 6.5 hit rule.
   - `async def grade_answer(question, answer) -> "correct" | "wrong" | "declined"`: used later by S4. For traps, `wrong` means a fabricated answer and `correct` means the premise was flagged.
   - Both use `JUDGE_MODEL` through `so.llm`.
2. `evals/matrix.py`: for all 330 questions, calls `search_engines(question, ENGINES)` with the question text as both query and goal, judges each engine's results, and writes `out/matrix.jsonl`. Then runs `search_default` once per question and writes `out/default_matrix.jsonl`.
   - Async, resumable, cached. Safe to re-run.
   - Prints a running count of spend. Expected total is about $12.
3. `evals/handcheck.py`: samples 30 matrix rows, shows the question, gold and results, and asks a human for yes or no. Writes agreement to `out/judge_agreement.json`.

Acceptance:

```
python -m evals.matrix --limit 10      # smoke test, a few cents
python -m evals.matrix                 # full run
python -m evals.matrix --summary
```

The summary prints hit rate per engine and for Telem's default, on learn and test separately. Post those numbers in `HANDOFF.md` the moment they exist. Oliver's checker lane is waiting for `out/matrix.jsonl`.

## S3: Rescue table, frontier and heatmap

Starts when the matrix is complete, about 12:15. Pure analysis, no paid calls.

Build:

1. `analysis/rescue_table.py`: from learn-split rows only, computes for each pair of engines how often B hit when A missed. Writes `out/rescue_table.json` in the section 6.6 format. `order_after[A]` sorts engines by rescue rate, breaking ties by lower price.
   - It must refuse to read test rows.
   - Print the table with counts, so thin cells are visible.
2. `analysis/frontier.py`: on test-split rows, computes coverage and cost per question for every combination of the six engines (63 combinations). Adds one point for Telem's default from `out/default_matrix.jsonl`. Adds the Second Opinion point from `out/results.json` when that exists, else a simulated point from the matrix marked `simulated: true`. Writes `out/frontier.json` and `out/frontier.png`.
   - Vertical axis label: "answer found in results". Horizontal: "search cost per question".
   - Telem's default is a labelled grey dot. Second Opinion is a labelled colored dot. The efficient frontier is a line.
3. `analysis/heatmap.py`: engine-by-engine rescue rates as `out/rescue_heatmap.json` and `.png`, with the count in each cell.
4. `analysis/findings.md`: three plain sentences on what the data shows. Include the most surprising rescue pair, and say whether the learned order beats a fixed order by more than noise.

Acceptance:

```
python -m analysis.rescue_table && python -m analysis.frontier && python -m analysis.heatmap
```

All files in section 6.13 exist and the PNGs are legible at slide size.

## S4: Eval runner and scoring

Depends on `so.agent.run_question`, due on `main` at 1:00. Build against a fake until then.

Build:

1. `evals/run_eval.py`: runs `run_question` for every test question and trap in each of the three setups. That is 230 questions times 3.
   - Concurrency from `LLM_MAX_CONCURRENCY`. Resumable: skips any question and setup that already has a finished log.
   - Flags: `--limit N`, `--setups`, `--traps-only`.
   - Prints progress, spend so far, and an estimate of time left.
2. `evals/score.py`: grades each final answer with `grade_answer`, totals the logs with `so.eventlog.summarize`, and writes `out/results.json` in the section 6.12 format.
   - Checker metrics: join each `check` event on a first-engine search with the matrix row for that question and engine. Compute miss recall and false alarm rate.
   - Judge model cost is excluded from every setup's cost.
   - Also writes `out/results.md`: the table in plain markdown, with counts beside every percentage.
3. A paired comparison in `out/results.md`: for `second_opinion` against each baseline, how many questions flipped from wrong to correct and from correct to wrong.

Schedule:

- 2:00: dry run on 20 questions. Fix anything that breaks.
- 2:30: full run starts. This is a hard deadline. If the full set will not finish by 3:10, cut to 120 test questions plus all traps and say so in the results.
- 3:15: `out/results.json` final. Re-run S3's frontier so the real Second Opinion point replaces the simulated one.

Acceptance:

```
python -m evals.run_eval --limit 20 && python -m evals.score
```

## S5: Pitch pack

Starts at 2:30 with placeholders. Fills numbers when results land.

Build, all in `pitch/`:

1. `devpost.md`: title, one-line tagline, what it does, how we built it, what we measured, what is next. Under 400 words. The ask is a sequential routing mode for Telem.
2. `script_90s.md`: the table pitch.
   - 0 to 10 seconds: the hook. "Telem checks every page it fetches and retries when the check fails. Search gets no check. We built it."
   - 10 to 55 seconds: the live race on a golden question.
   - 55 to 90 seconds: the frontier chart and the one held-out number.
3. `script_3min.md`: the stage pitch. Problem, the kinds of miss, the race, the numbers, who pays, the ask.
4. `qa.md`: short answers to the likely questions.
   - Is this just routing? Routing picks engines before results. This decides after.
   - How do you know the answer is missing without knowing the answer?
   - What does the check cost in money and time?
   - Could Telem build this? Yes. Here is the spec and the data that says they should.
   - Did you test on the same questions you tuned on? No. Learn and test are separate.
   - What about prior work? Name the Google sufficient-context study and the 2026 paper on oracle unions across search providers. Ours is live, needs no gold answer, and picks the next engine from measured data.
5. `slides.md`: four slides at most. Problem, how it works, results table, frontier chart.
6. `fill_numbers.py`: replaces `{{placeholders}}` in these files from `out/results.json`.

Rules:

- Every number carries its question count.
- Say "no accuracy loss", never "more accurate than Telem".
- No em dashes.

## Shikhar, by hand

- 11:00: join the repo, copy `.env`, start S1.
- 11:30: read all 30 traps. Delete any you would not defend to an investor.
- 12:15: run `python -m evals.handcheck` and label the 30 samples yourself. This takes about ten minutes and is the number that makes the judge credible.
- 2:30: with Oliver, choose the main story or the fallback.
- 3:30 to 4:10: record a 60 to 90 second screen capture of the race, fill in Devpost, and submit by 4:10.
