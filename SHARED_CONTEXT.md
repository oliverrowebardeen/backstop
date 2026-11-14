# Second Opinion: shared context

Every agent reads this file fully before touching code. Then it works only on the lanes in its owner's document: `OLIVER.md` or `SHIKHAR.md`.

## 1. What we are building

Second Opinion is a layer between an AI agent and Telem's search API. Telem is a router in front of about a dozen web search engines. By default Telem asks three engines per search, chosen before any results exist. Nothing checks whether the results contain the answer.

Second Opinion asks one engine, has a small model check the results against the agent's stated goal, and then acts:

- If the answer is there, it hands the results back and tells the agent to stop.
- If the search missed, it applies a fixed rescue action for that kind of miss, using a measured table of which engine rescues which.
- If two rescues fail, it returns "not found" so the agent does not guess.

The claim we must be able to defend with held-out numbers:

1. Fewer confidently wrong answers than a single-engine agent.
2. Accuracy tied with Telem's default three engines.
3. Total cost 35 to 50 percent lower than Telem's default, with the checker's own cost counted.

## 2. Event constraints

- SearchHack, Saturday Nov 14, 2026. Build window 11:00 to 4:15 PT.
- Feature freeze 3:30. Devpost submission by 4:10. Judging at 4:30.
- No project code before 11:00.
- Judged on innovation, technical execution, search intelligence, impact. The judges are investors.
- Team: Oliver and Shikhar. Each runs several coding agents in parallel lanes.

## 3. Vocabulary

| Term | Meaning |
|---|---|
| Engine | One search provider behind Telem, for example `tavily` or `exa` |
| Hit | The gold answer appears in the title or snippet of a top-5 result |
| Miss | Not a hit |
| Verdict | The checker's label for one result list: `HIT`, `STALE`, `WRONG_ENTITY`, `SHALLOW`, `DEAD` |
| Rescue | One extra action after a miss: retry on another engine, fetch a page, or reword |
| Setup | One way of running the agent: `single`, `telem_default`, `second_opinion` |
| Trap | A question with a false premise, where the right answer is to say so |
| Learn split | Questions used to build the rescue table and tune the checker |
| Test split | Questions used for every reported number. Never tuned on. |

## 4. Stack

- Python 3.11+, `asyncio`, `pydantic` v2, `httpx`.
- Telem via `telem-sdk` (`pip install telem-sdk`). Key in `TELEM_API_KEY`.
- Server: `fastapi` + `uvicorn`, server-sent events.
- UI: plain HTML, CSS and JavaScript. No build step, no framework.
- Charts: `matplotlib` for PNG, plus the same data as JSON for the UI.
- All model calls go through `so/llm.py`. No lane calls a model SDK directly.

### Telem SDK facts (verified against docs on Nov 14)

```python
from telem import Telem, AsyncTelem

async with AsyncTelem() as client:
    resp = await client.search(
        "query text",                 # a string, or a list of strings for a batch
        providers_include=["tavily"],
        num_results=5,                # per engine, 1 to 20
        goal="why the agent is searching",
        published_after="2026-10-01", # optional
        session=None,                 # pass a previous session_id to group calls
    )
```

- `resp.by_provider` is a list of runs. Each run has `provider`, `status`, `results`, `error`, `latency_ms`, `query`, `batch_index`.
- Each result has `url`, `title`, `summary`, `excerpt`, `full_content`, `publish_date`, `rank`, `provider`. The `content` property returns the best available text.
- `auto_routing="accuracy"` turns on Telem's default routing. Leave `providers_include` unset for that call.
- `client.providers()` lists valid engine ids. Known ids: `brave`, `ceramic`, `exa`, `linkup`, `parallel`, `perplexity`, `seltz`, `serpapi`, `tavily`, `tinyfish`, `you`.
- Fetch reads up to five URLs per call. The exact method name was not confirmed. Check https://docs.telem.ai/sdks/python/ before writing `fetch_page`.
- The SDK does not retry. Handle 429 and 5xx yourself.
- The response has no cost field. Compute cost from the price table in `so/config.py`.
- REST fallback: `POST https://router.telem.ai/v1/search` and `POST https://router.telem.ai/v1/fetch`.

## 5. Repo layout and ownership

An agent may only create or edit files in directories its owner controls. If you need a change elsewhere, write the request in `HANDOFF.md` under your lane name and tell your human.

```
second-opinion/
  SHARED_CONTEXT.md  OLIVER.md  SHIKHAR.md  README.md  HANDOFF.md
  pyproject.toml  .env.example                      Oliver (O0)
  so/                                               Oliver
    contracts.py     pydantic models for section 6  O0, frozen at 11:15
    config.py        env, engine list, price table  O0
    telem_client.py  cached Telem wrapper           O0
    llm.py           model wrapper                  O0
    eventlog.py      JSONL event writer             O0
    checker.py  checker_eval.py                     O1
    policy.py  second_opinion.py  memory.py         O2
    agent.py  setups.py                             O3
  server/            FastAPI app and SSE            O4
  ui/                race screen and results view   O4
  demo/              golden questions and replays   O5
  data/              question files and loaders     Shikhar (S1)
  evals/             matrix, judge, eval runner     Shikhar (S2, S4)
  analysis/          rescue table, frontier, charts Shikhar (S3)
  pitch/             Devpost text, scripts, slides  Shikhar (S5)
  tests/<lane>/      each lane's own tests
  out/               generated artifacts
  cache/             raw API responses, git-ignored
```

`out/` is written by both sides. Each file in it has exactly one writer, named in section 6.

## 6. Contracts

These are frozen at 11:15. `so/contracts.py` holds them as pydantic models. Import from there. Do not redefine them.

### 6.1 Question

Files: `data/questions.jsonl` (300 lines) and `data/traps.jsonl` (30 lines). Writer: S1.

```json
{"id": "sqa-0001", "question": "...", "gold": ["answer", "alias"],
 "category": "fact", "source": "simpleqa", "split": "learn",
 "is_trap": false, "trap_note": null}
```

- `category`: `fact`, `finance`, or `trap`.
- `split`: 100 `learn`, 200 `test`, stratified by category, random seed 14.
- Traps are all `test`. Their `gold` describes the correct behavior. `trap_note` holds the source URL that proves the premise false.

### 6.2 Engines and prices

```python
ENGINES = ["tavily", "exa", "brave", "perplexity", "parallel", "ceramic"]
FIRST_ENGINE = "tavily"
SUBSTITUTES = ["seltz", "you", "linkup"]   # if one of the six is unavailable
PRICE_USD = {"tavily": 0.005, "brave": 0.005, "you": 0.005,
             "parallel": 0.001, "ceramic": 0.00005, "tinyfish": 0.0}
DEFAULT_PRICE_USD = 0.005   # for any engine missing above
```

O0 confirms ids with `client.providers()` and fills missing prices from https://docs.telem.ai/pricing/.

### 6.3 Result

```json
{"rank": 1, "title": "...", "url": "https://...", "snippet": "...",
 "published": "2026-10-02", "engine": "tavily"}
```

`snippet` is `summary`, else `excerpt`, else empty, cut to 500 characters.

### 6.4 Telem wrapper (`so/telem_client.py`, writer O0)

```python
async def search_engines(query: str, engines: list[str], *, num_results: int = 5,
                         goal: str | None = None, published_after: str | None = None,
                         session: str | None = None) -> dict[str, EngineRun]
async def search_default(query: str, *, goal: str | None = None,
                         session: str | None = None) -> dict[str, EngineRun]
async def fetch_page(url: str) -> PageText
```

- `EngineRun`: `{engine, status: "ok"|"error", results: list[Result], latency_ms, cost_usd, cached}`.
- `PageText`: `{url, ok, text, latency_ms, cost_usd, cached}`. `text` is cut to 6,000 characters.
- `search_default` uses `auto_routing="accuracy"` and returns one `EngineRun` per engine Telem chose.
- Every raw response is cached under `cache/` keyed by a hash of the call. `SO_CACHE=readwrite` (default) or `off`. Cache hits report `cost_usd` and `latency_ms` from the original call and set `cached: true`.
- Retries: up to 3 on 429 and 5xx, with backoff.
- Concurrency: a global semaphore, `TELEM_MAX_CONCURRENCY`, default 16.
- Budget: stop and raise when spend passes `TELEM_BUDGET_USD`, default 25.

### 6.5 Hit matrix

File: `out/matrix.jsonl`, one line per question and engine. Writer: S2.

```json
{"question_id": "sqa-0001", "engine": "exa", "hit": true, "hit_rank": 2,
 "judge_reason": "...", "n_results": 5, "latency_ms": 640, "cost_usd": 0.005}
```

A second file, `out/default_matrix.jsonl`, has the same shape with `engine: "telem_default"` and an extra `engines_used` list.

Hit rule: a judge model that sees the gold answer decides whether any top-5 title or snippet contains it. Lenient on formatting, strict on the entity.

### 6.6 Rescue table

File: `out/rescue_table.json`. Writer: S3. Built on the learn split only.

```json
{"built_on_split": "learn", "n_questions": 100,
 "miss_counts": {"tavily": 31},
 "rescue": {"tavily": {"exa": {"rescued": 9, "misses": 31, "rate": 0.29}}},
 "order_after": {"tavily": ["exa", "perplexity", "brave", "parallel", "ceramic"]},
 "fallback_order": ["exa", "perplexity", "brave", "parallel", "ceramic"]}
```

Until this file exists, the policy uses `fallback_order` from `so/config.py`.

### 6.7 Checker verdict

```json
{"verdict": "WRONG_ENTITY", "confidence": 0.82, "why": "results cover a different company",
 "evidence_rank": null, "best_url": null, "rewrite": "disambiguated query text"}
```

| Verdict | Meaning |
|---|---|
| `HIT` | A top-5 title or snippet states the answer to the goal |
| `STALE` | Results are on topic, but the newest evidence predates what the goal needs |
| `WRONG_ENTITY` | Results are about a similarly named but different entity |
| `SHALLOW` | A result is clearly the right page, but the answer is not in the snippet |
| `DEAD` | Nothing relevant |

The checker sees the query, the goal, the top 5 results and today's date. It never sees gold answers. Only `HIT` versus not-`HIT` is scored. The four miss types are explanations that select an action.

### 6.8 Rescue actions (fixed, not learned)

| Verdict | Action |
|---|---|
| `HIT` | Return results. Tell the agent to stop. |
| `SHALLOW` | Fetch `best_url` and re-check on the page text |
| `STALE` | Retry on the next engine from the rescue table, with `published_after` set and the checker's `rewrite` |
| `WRONG_ENTITY` | Retry on the next engine from the rescue table, using the checker's `rewrite` |
| `DEAD` | Retry once on the next engine from the rescue table with the checker's `rewrite` |

At most 2 rescues per search call. After that the outcome is `NOT_FOUND`. An engine is never retried within one call.

### 6.9 Search tool

This is the only thing the agent calls. The three setups share the signature.

```python
async def search(query: str, goal: str, ctx: RunContext) -> SearchOutcome
```

```json
{"status": "HIT", "results": [], "note": "Answer is in result 2. You can stop searching for this.",
 "engines_called": ["tavily", "exa"], "trace": [], "calls": 2, "fetches": 0,
 "cost_usd": 0.012, "latency_ms": 1900}
```

- `status`: `HIT`, `NOT_FOUND`, or `UNCHECKED`.
- `single`: Tavily only, no check, status `UNCHECKED`.
- `telem_default`: `search_default`, no check, status `UNCHECKED`.
- `second_opinion`: the full loop.
- On `NOT_FOUND` the note reads: "No engine surfaced this after N tries. If you cannot answer from other evidence, answer NOT FOUND."

### 6.10 Agent

- Same model, same system prompt and same tools in all three setups. Only the search tool differs.
- Tools: `search(query, goal)` and `read_page(url)`.
- At most 6 search calls per question.
- The system prompt allows "NOT FOUND" as a final answer in every setup, so the comparison is fair.

```json
{"final": "answer text or NOT FOUND", "status": "ANSWERED", "citations": ["https://..."], "searches": 3}
```

`status` is `ANSWERED` or `NOT_FOUND`.

### 6.11 Event log

File: `out/logs/<run_id>.jsonl`, one event per line. Writer: whoever runs the agent, through `so/eventlog.py`. `run_id` is `<setup>-<question_id>-<short uuid>`.

```json
{"ts": "2026-11-14T19:05:11.201Z", "run_id": "...", "question_id": "sqa-0001",
 "setup": "second_opinion", "seq": 4, "type": "check", "data": {}}
```

| `type` | `data` fields |
|---|---|
| `run_start` | `question` |
| `search` | `query`, `goal`, `engines`, `n_results`, `latency_ms`, `cost_usd`, `cached` |
| `check` | `verdict`, `confidence`, `why`, `evidence_rank` |
| `rescue` | `action` (`RETRY_ENGINE`, `FETCH_PAGE`), `engine`, `query`, `reason` |
| `fetch` | `url`, `ok`, `latency_ms`, `cost_usd` |
| `search_done` | `status`, `calls`, `fetches`, `cost_usd`, `latency_ms`, `note` |
| `llm` | `role` (`agent`, `checker`, `judge`), `model`, `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms` |
| `answer` | `final`, `status`, `citations` |
| `run_end` | `engine_calls`, `fetches`, `searches`, `cost_usd_search`, `cost_usd_llm`, `cost_usd_total`, `latency_ms` |

The race screen and the eval both read these files. A recorded log replays as a live run.

### 6.12 Results

File: `out/results.json`. Writer: S4.

```json
{"generated_at": "...", "n_test": 200, "n_traps": 30,
 "setups": {"single": {}, "telem_default": {}, "second_opinion": {}},
 "checker": {"n_searches": 0, "miss_recall": 0.0, "false_alarm_rate": 0.0,
             "rescuable_vs_dead_acc": 0.0},
 "judge_agreement": {"n_checked": 30, "agree": 0}}
```

Each setup block: `correct`, `wrong`, `declined`, `accuracy`, `wrong_rate`, `engine_calls_per_q`, `cost_per_q_search`, `cost_per_q_total`, `latency_p50_ms`, `latency_p95_ms`, `trap_n`, `trap_confident_wrong`.

### 6.13 Frontier and heatmap

Files: `out/frontier.json`, `out/frontier.png`, `out/rescue_heatmap.json`, `out/rescue_heatmap.png`. Writer: S3.

```json
{"split": "test", "points": [
  {"kind": "combo", "engines": ["tavily", "exa"], "coverage": 0.77, "cost_per_q": 0.010},
  {"kind": "telem_default", "engines": [], "coverage": 0.80, "cost_per_q": 0.015},
  {"kind": "second_opinion", "engines": [], "coverage": 0.78, "cost_per_q": 0.009}]}
```

The vertical axis is labelled "answer found in results". It is coverage, not final answer accuracy.

### 6.14 Server API (writer O4)

| Route | Purpose |
|---|---|
| `POST /race` with `{question, left, right}` | Starts two setups on one question. Returns `{left_run_id, right_run_id}`. |
| `GET /stream/{run_id}` | Server-sent events, one per log event |
| `GET /replay/{name}` | Streams a recorded pair from `demo/` with original timing |
| `GET /results` | Serves `out/results.json` and `out/frontier.json` |
| `GET /` | The UI |

## 7. Metric definitions

- **Correct, wrong, declined:** a judge model with the gold answer grades each final answer. `NOT FOUND` is `declined`.
- **Wrong rate:** wrong divided by all questions. This is the headline number.
- **Trap confident wrong:** on a trap, the agent states a fabricated answer instead of flagging the premise.
- **Engine calls per question:** every engine query Telem bills, summed over the run.
- **Total cost:** engine calls plus fetches plus every model call, checker included.
- **Miss recall:** of searches that truly missed (per the matrix), the share the checker labelled not-`HIT`.
- **False alarm rate:** of searches that truly hit, the share the checker labelled not-`HIT`.
- **Latency:** wall clock per question, median and 95th percentile. Measured with the cache off or from original timings.

## 8. Rules that keep the numbers defensible

1. The rescue table and all checker tuning use the learn split only.
2. Every reported number comes from the test split or the traps.
3. The checker never sees gold answers.
4. The checker's cost and latency are always included in our totals.
5. We claim "no accuracy loss" against Telem's default, never "more accurate".
6. Trap results are reported separately with the count shown.
7. Anything replayed in the demo is labelled as a replay of a live run.

## 9. Git workflow

- One repo. Oliver creates it at 11:00 and adds Shikhar.
- No pull requests. They cost time we do not have. Nobody opens one, and nobody waits for a review before merging.
- Each lane works on its own branch: `lane/o2-policy`, `lane/s3-analysis`, and so on.
- A lane merges its own branch straight into `main` and pushes when its acceptance check passes. Rebase on `main` first.
- Review happens after the merge, in Oliver's O6 lane, and never blocks anyone.
- A lane never edits files outside its directories, so merges should not conflict.
- Commit small and often. Never commit `.env`, `cache/` or keys.
- If `main` breaks, the lane that broke it fixes it before doing anything else.

## 10. Timeline and checkpoints

| Time | Checkpoint | Owner |
|---|---|---|
| 11:00 | Repo created. O0 and S1 start. | Both |
| 11:15 | `so/contracts.py` merged. Contracts frozen. | Oliver |
| 11:20 | Telem wrapper, model wrapper and event log merged | Oliver |
| 11:40 | Question files merged | Shikhar |
| 12:15 | Hit matrix complete for 330 questions on 6 engines | Shikhar |
| 12:30 | Checker scored on the learn split. Rescue table v1 written. | Both |
| 1:00 | Agent runs in all three setups on 5 questions | Oliver |
| 1:15 | Frontier chart and heatmap written | Shikhar |
| 1:30 | Second Opinion works end to end | Oliver |
| 2:00 | Race screen streams a live question. Eval dry run on 20 questions passes. | Both |
| 2:30 | Full eval starts. Decide main story or fallback. | Both |
| 3:15 | `out/results.json` final. Golden questions recorded. | Both |
| 3:30 | Feature freeze | Both |
| 4:10 | Devpost submitted | Shikhar |

Fallback, decided at 2:30: if miss recall on the learn split is under 70 percent, we drop the savings claim and present Second Opinion as a diagnostic that labels every failed search by kind of miss.

## 11. Environment

`.env.example`, written by O0:

```
TELEM_API_KEY=
LLM_PROVIDER=anthropic          # or openai
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
CHECKER_MODEL=claude-haiku-5-5
AGENT_MODEL=claude-sonnet-5-5
JUDGE_MODEL=claude-sonnet-5-5
SO_CACHE=readwrite
TELEM_MAX_CONCURRENCY=16
TELEM_BUDGET_USD=25
LLM_MAX_CONCURRENCY=8
```

Runtime model calls need an API key. If the provider is OpenAI, set the three model names explicitly.

## 12. Rules for every agent

1. Read this file, then your lane in your owner's document. Do nothing else.
2. Import contracts from `so/contracts.py`. If a contract is wrong, stop and tell your human.
3. Stay inside your directories.
4. If a dependency is not merged yet, code against the interface here with a small fake in `tests/<lane>/`.
5. Cache every paid call. Never loop over paid calls without the budget guard.
6. Each lane ships a one-command acceptance check, listed in its lane description.
7. No new dependencies without asking your human.
8. No em dashes in any user-facing text.
9. Never open a pull request. Merge your branch directly into `main` and push.
10. When done, append three lines to `HANDOFF.md`: what you built, how to run it, what is not done.
