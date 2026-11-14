# Oliver's lanes

Read `SHARED_CONTEXT.md` first. This file covers the core library, the agent, the race screen and integration. Shikhar's agents own data, evals, analysis and the pitch pack. Do not touch `data/`, `evals/`, `analysis/` or `pitch/`.

## Lane map

| Lane | What | Agent | Window | Directories |
|---|---|---|---|---|
| O0 | Foundation | Codex | 11:00 to 11:20 | repo root, `so/contracts.py`, `so/config.py`, `so/telem_client.py`, `so/llm.py`, `so/eventlog.py` |
| O1 | Checker | Claude | 11:10 to 12:30 | `so/checker.py`, `so/checker_eval.py`, `so/prompts/` |
| O2 | Policy and Second Opinion | Codex | 11:20 to 1:15 | `so/policy.py`, `so/second_opinion.py`, `so/memory.py` |
| O3 | Agent loop and setups | Codex | 11:20 to 1:00 | `so/agent.py`, `so/setups.py` |
| O4 | Server and race screen | Codex | 12:00 to 2:00 | `server/`, `ui/` |
| O5 | Integration and demo | Claude | 1:00 to 3:30 | `demo/`, `README.md`, `HANDOFF.md`, fixes anywhere in `so/` |
| O6 | Review | Codex | 1:30 to 3:15 | read-only, writes `REVIEW.md` |

Five lanes run on Codex and two on Claude. Keep the Claude lanes off Fable, since that limit is at 82 percent.

## Kickoff message for each lane

Paste this, with the lane filled in:

```
Read SHARED_CONTEXT.md fully, then OLIVER.md. You are lane <ID>. Build only what
that lane lists, only in its directories. Code against the contracts in
so/contracts.py. If a dependency is not merged, use a fake in tests/<lane>/.
Work on branch lane/<id>-<name>. When the acceptance check passes, merge straight
into main and push. Do not open a pull request. Then append your three lines to
HANDOFF.md and stop.
```

## O0: Foundation (Codex)

Everything else blocks on this. Push `so/contracts.py` first, by 11:10, on its own commit.

Build:

1. Repo skeleton from section 5 of the shared doc, `pyproject.toml`, `.gitignore`, `.env.example`.
2. `so/contracts.py`: pydantic v2 models for every contract in section 6. Names: `Question`, `Result`, `EngineRun`, `PageText`, `MatrixRow`, `RescueTable`, `CheckerVerdict`, `SearchOutcome`, `AgentAnswer`, `Event`, `Results`, `Frontier`, `RunContext`.
3. `so/config.py`: loads `.env`, exposes `ENGINES`, `FIRST_ENGINE`, `PRICE_USD`, `fallback_order`, model names.
4. `so/telem_client.py`: the three functions in section 6.4, with cache, retries, semaphore and budget guard. Confirm engine ids with `client.providers()` and the fetch method name against the Telem docs.
4a. `so/tavily_client.py`: `tavily_extract(url) -> PageText`, calling Tavily Extract directly with `TAVILY_API_KEY`. Cached and budgeted. See "Tavily use" in the shared doc. Verify the SDK call against the Tavily docs first.
5. `so/llm.py`: `async def complete(role, system, messages, *, tools=None, json_schema=None) -> LLMResponse`. Supports Anthropic and OpenAI by `LLM_PROVIDER`. Returns text, tool calls, token counts, cost and latency. Has its own semaphore.
6. `so/eventlog.py`: `EventLog(run_id, question_id, setup)` with `emit(type, **data)`, writing section 6.11 lines. Also `read_log(path)` and `summarize(path)` returning the `run_end` totals.

Acceptance:

```
python -m so.telem_client "who founded Tavily" --engines tavily exa
```

Prints normalized results from both engines. A second run reports `cached: true`.

## O1: Checker (Claude)

This lane decides whether the project works. Start at 11:10 from the contract alone.

Build:

1. `so/checker.py`: `async def check(query, goal, results, today) -> CheckerVerdict`. One model call on `CHECKER_MODEL`, JSON output, under 400 output tokens. Also `async def check_page(query, goal, page_text, today) -> CheckerVerdict` for the `SHALLOW` path.
2. `so/prompts/checker.md`: the prompt. It defines the five verdicts exactly as in section 6.7 and includes two short examples of each.
3. `so/checker_eval.py`: joins `out/matrix.jsonl` with the cached Tavily results for the learn split, runs the checker, and prints miss recall, false alarm rate and a confusion table of verdicts. Flag `--split learn` only. It must refuse to run on `test`.

Guidance:

- When unsure, do not say `HIT`. A wrong `HIT` loses the answer. A wrong miss costs one extra call.
- Tune only against the learn split. Stop tuning at 12:45 whatever the score.
- Until the matrix exists, work from 15 hand-made examples in `tests/o1/`.

Acceptance:

```
python -m so.checker_eval --split learn
```

Target: miss recall at or above 0.85 with false alarms at or below 0.25. Report the real numbers either way in `HANDOFF.md`.

## O2: Policy and Second Opinion (Codex)

Build:

1. `so/memory.py`: per-run record of queries issued, engines tried per query, and URLs seen.
2. `so/policy.py`: `next_action(verdict, tried_engines, rescues_used, table) -> Action`. Implements section 6.8 exactly. Loads `out/rescue_table.json` if present, else `fallback_order`.
3. `so/second_opinion.py`: `async def search(query, goal, ctx) -> SearchOutcome`. The loop: ask `FIRST_ENGINE`, check, act, at most 2 rescues, then `NOT_FOUND`. Emits `search`, `check`, `rescue`, `fetch` and `search_done` events. Merges and de-duplicates results by URL across engines.

Rules:

- For `STALE`, set `published_after` to 90 days before today unless the goal names a date.
- A `SHALLOW` rescue reads one page with `tavily_extract`, falling back to Telem's `fetch_page` if that fails, then calls `check_page`. It counts as one rescue. Set `via` on the `fetch` event.
- Never call the same engine twice inside one search call.
- Unit tests use a fake checker and a fake Telem client. Cover every row of the action table and the two-rescue cap.

Acceptance:

```
pytest tests/o2 -q
python -m so.second_opinion "What was Stripe's valuation in its 2023 round?" --goal "find the valuation figure"
```

## O3: Agent loop and setups (Codex)

Build:

1. `so/setups.py`: `get_search_tool(setup)` returning the right `search` for `single`, `telem_default` and `second_opinion`. The first two wrap the Telem client and return status `UNCHECKED`.
2. `so/agent.py`: `async def run_question(question: Question, setup: str, *, log_dir="out/logs") -> AgentAnswer`. A tool-calling loop on `AGENT_MODEL` with `search(query, goal)` and `read_page(url)`. At most 6 searches. Emits `run_start`, `llm`, `answer` and `run_end`.
3. One system prompt for all setups in `so/prompts/agent.md`. It tells the agent to give a goal with every search, to cite URLs, to heed tool notes, and to answer "NOT FOUND" when the evidence is not there.

Open one Telem session per run. Carry its id in `RunContext` and pass it on every Telem call in that run. Pass the agent's `goal` on every search.

Until O2 merges, `second_opinion` may point at `single` behind a flag.

Acceptance:

```
python -m so.agent --setup single --question "Who is the CEO of Tavily?"
python -m so.agent --setup telem_default --question "Who is the CEO of Tavily?"
```

Each prints an `AgentAnswer` and writes a log that `so.eventlog.summarize` can total.

## O4: Server and race screen (Codex)

Start at 12:00 against a fixture log. Do not wait for the agent.

Build:

1. `server/app.py`: the routes in section 6.14.
2. `ui/index.html`, `ui/app.js`, `ui/style.css`: two views.
   - **Race:** a question box and a "Run race" button. Two columns, left and right setup. Each column has three large counters (engine calls, cost, time), a list of search steps showing the query, engine chips and the verdict in words, and the final answer. A "not found" answer is shown as a deliberate outcome, not an error.
   - **Results:** the table from `out/results.json` with the question counts printed, plus the frontier chart drawn from `out/frontier.json`.
3. A setup picker per column. Default is `single` on the left and `second_opinion` on the right.
4. Replay: a dropdown of recorded pairs in `demo/`. Replays carry a visible "replay of a live run" label.

Design rules:

- Readable from two meters. Counters at least 40px. One screen, no scrolling for a typical run.
- Verdicts are words with color as a second cue: Hit, Stale, Wrong entity, Shallow, Dead, Not found.
- Telem use is 40 percent of the score. Show each engine called as a chip, a running "Telem calls" counter per column, and a small "search and fetch by Telem" line. If a run has a Telem session id, show it so it can be opened in Telem's console.
- Tavily use is required. Tavily appears as the first engine chip on every search, and a page read shows "read by Tavily Extract" or "read by Telem fetch".
- Show the final answer's source links.
- Add an "Export audit trail" button per column that downloads the run's event log as JSON. Regulated buyers need a record of what was searched, what was checked and why an answer was given. This supports the venture story and costs almost nothing, since the log already exists.
- Light and dark both legible. No frameworks, no build step.

Acceptance:

```
uvicorn server.app:app --port 8000
```

Opening `/` and choosing a fixture replay animates both columns and ends with totals that match the logs.

## O5: Integration and demo (Claude)

Starts at 1:00.

1. Merge order: O0, then O3, O2, O1, O4. Run each lane's acceptance check on `main`.
2. End-to-end check on 5 learn questions in all three setups. Fix contract mismatches in `so/`.
3. Golden questions, in `demo/golden.json`: find three that show the story on live runs.
   - One where `single` answers confidently and wrong and `second_opinion` recovers through a retry.
   - One trap where `single` invents an answer and `second_opinion` returns "not found".
   - One `SHALLOW` rescue through a page fetch.
   Candidates come from Shikhar's test split and traps after the eval has run, so no number is tuned on them.
4. Record each golden pair to `demo/` for replay.
5. `README.md`: what it is, the one diagram, how to run the race and the eval, the results table.
6. From 2:30, triage only. No new features.

## O6: Review (Codex)

Read-only. From 1:30, review merged code against the contracts and the honesty rules in section 8. Check in particular:

- The checker cannot see gold answers anywhere in the call path.
- `checker_eval` and the rescue table never read the test split.
- Cost totals include checker and judge calls in the right buckets. Judge cost is excluded from setup costs.
- Latency is not measured from cache hits unless original timings are used.

Write findings to `REVIEW.md`, most serious first. Do not fix anything.

## Oliver, by hand

- 11:00: create the repo, add Shikhar, put keys in `.env`, start O0.
- 11:10: start O1 as soon as `contracts.py` lands. Start O2 and O3 at 11:20.
- 12:30: read the checker's learn-split score. This is the first real signal.
- 2:30: with Shikhar, choose the main story or the fallback.
- 3:00: pick the golden questions with O5 and rehearse the 90-second version.
- You take Q&A. Read `pitch/qa.md` when Shikhar's lane writes it.
