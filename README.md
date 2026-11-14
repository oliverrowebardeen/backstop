# Backstop

**Verified search for AI agents, built on Telem.**

Backstop sits between your agent and Telem's search API. It asks one engine, checks
whether the results actually answer what the agent needs, rescues misses on the engine
our data says catches them, and returns `NOT_FOUND` instead of letting the agent guess.
It is for teams running research and diligence agents, where a confident wrong fact
in a memo, a model or a customer answer costs real money. Backstop is open source,
installable, and production-ready: bounded in cost and time, fail-open, typed, and
fully traced.

## Quickstart

```sh
pip install -e .            # Python 3.11+; put TELEM_API_KEY and ANTHROPIC_API_KEY in .env
```

```python
from backstop import BackstopTelem

client = BackstopTelem()    # was: client = AsyncTelem()
resp = await client.search("What was Stripe's valuation in its 2023 round?", goal="find the valuation figure")
print(resp.status, resp.results[0].url, resp.results[0].rank_reason)   # HIT, the evidence URL, "evidence"
```

That one-line swap is the whole integration. The response keeps Telem's shape
(`results`, `by_provider`, `session_id`), so existing call sites work unchanged, and
adds `status`, `verdict`, `why`, `note`, `trace`, `engine_calls`, `cost_usd`,
`latency_ms` and a `rank_reason` on every result. Every other `AsyncTelem` method is
passed through.

## How it works on Telem

Default routing asks several engines per search, chosen before any results exist, and
nothing checks whether the answer came back. Backstop puts the decision after the
results instead.

```
agent ──search(query, goal)──▶ Telem: one pinned engine ──▶ checker
                                                             │
          HIT ◀── return ranked results, tell the agent to stop
          miss ─▶ rescue: read the page, or retry on the next engine with a rewritten query
          two failed rescues ─▶ NOT_FOUND: "do not guess"
```

**Query understanding.** The checker reads the query, the agent's goal, today's date
and the top 5 results, and says why a search missed: wrong entity, stale, shallow, or
nothing relevant. Each miss comes with a rewritten query aimed at that failure, and
the rewrite is recorded in the trace as a before and after. An illustrative example:

| | Query |
|---|---|
| Before (agent) | `Mercury funding round valuation` |
| Miss | `WRONG_ENTITY`: results cover Mercury Insurance, not the fintech |
| After (rewrite) | `Mercury fintech banking startup Series C valuation` |

**Retrieval.** Every search goes through one Telem API with pinned engines
(`providers_include`), the agent's `goal`, one Telem `session` per run so all calls are
grouped in Telem's console, `published_after` for stale retries, and Telem fetch as the
page-read fallback. The rescue order (which engine to try after a given engine misses)
comes from a hit matrix: every question asked on six engines through the same API, in
one result format. That matrix is the part that is hard to build without Telem.

**Ranking.** Results from every engine called are merged and deduplicated by URL,
then ranked: the result the checker cited as evidence first, then results that more
than one engine returned (more engines first), then the best original engine rank.
Each result says why it is where it is: `evidence`, `agreement: 2 engines`, `engine rank 3`.

**Grounded results.** A `HIT` points at the exact result holding the answer, so the
agent can cite its URL. A miss that survives two rescues becomes `NOT_FOUND`, with a
note telling the agent to answer NOT FOUND unless it has other evidence. The checker's
cost is always counted in `cost_usd`.

### Verdicts and rescue actions

| Verdict | Meaning | Action |
|---|---|---|
| `HIT` | A top-5 title or snippet states the answer | Return results, tell the agent to stop |
| `SHALLOW` | The right page is there, but the snippet lacks the answer | Read the page with Tavily Extract (Telem fetch as fallback) and check again |
| `STALE` | On topic, but older than the goal needs | Retry on the next engine with `published_after` and the rewrite |
| `WRONG_ENTITY` | About a similarly named entity | Retry on the next engine with the rewrite |
| `DEAD` | Nothing relevant | Retry once on the next engine with the rewrite |

At most two rescues per search, and an engine is never asked twice in one search.

### Outcomes

| Status | Meaning |
|---|---|
| `HIT` | The answer is in the results. `note` says which result. |
| `NOT_FOUND` | No engine surfaced it within the limits. The agent should not guess. |
| `UNCHECKED` | The checker failed or timed out. The results are returned unverified, with the reason. |

## Configuration

Set in `.env` or the environment; the limits can also be set on the client or per call.

| Setting | Default | Meaning |
|---|---|---|
| `TELEM_API_KEY` | | Telem key |
| `TELEM_BASE_URL` | `https://router.telem.ai` | Telem router |
| `ANTHROPIC_API_KEY` | | Key for the checker model |
| `CHECKER_MODEL` | `claude-haiku-5-5` | Model that labels each search |
| `FIRST_ENGINE` | `tavily` | Engine every search starts on |
| `FALLBACK_ORDER` | `exa,perplexity,brave,parallel,ceramic` | Rescue order when `out/rescue_table.json` is absent |
| `MAX_RESCUES` / `max_rescues` | 2 | Rescues per search |
| `SEARCH_MAX_COST_USD` / `max_cost_usd` | 0.03 | Spend cap per search, checker included |
| `SEARCH_TIMEOUT_S` / `timeout_s` | 12 | Wall-clock budget per search |
| `CHECKER_TIMEOUT_S` / `checker_timeout_s` | 4 | Slower checks fail open as `UNCHECKED` |
| `TELEM_BUDGET_USD`, `LLM_BUDGET_USD` | 25 | Process-wide spend guards |

```python
client = BackstopTelem(max_cost_usd=0.02)                     # client default
resp = await client.search("...", goal="...", timeout_s=20)   # per call
```

Engine errors are recorded and skipped without spending a rescue. Telem failing
entirely raises `backstop.TelemUnavailable`. See [docs/api.md](docs/api.md) for the
full reference.

## Hosted API

The same verified search is available over HTTP, with bearer keys, per-key usage
metering in SQLite and a per-key rate limit of 60 requests per minute.

```sh
echo "BACKSTOP_API_KEYS=bk_live_one" >> .env
uvicorn server.app:app --port 8000

curl -s http://localhost:8000/v1/search \
  -H "Authorization: Bearer bk_live_one" -H "Content-Type: application/json" \
  -d '{"query": "What was Stripe'\''s valuation in its 2023 round?", "goal": "find the valuation figure"}'

curl -s http://localhost:8000/v1/usage -H "Authorization: Bearer bk_live_one"
curl -s http://localhost:8000/health
```

Errors are JSON with the right status codes: 401 bad key, 422 bad body, 429 rate
limited, 502 Telem unavailable. The same server hosts the race screen at `/`, which
plays a single-engine agent and a Backstop agent side by side from their event logs.

## Results

Results land here after the held-out eval runs.

## How we measured

The evaluation lives in [`evals/`](evals/) with question files in [`data/`](data/) and
analysis in [`analysis/`](analysis/).

- Questions are split once, stratified, with a fixed seed: a **learn** split and a
  held-out **test** split, plus a separate set of false-premise traps.
- The rescue table (`out/rescue_table.json`) and all checker tuning use the **learn
  split only**. Every reported number comes from the **held-out test split** or the
  traps, which are reported separately with their count.
- Search results that copy the benchmark are filtered out before judging:
  [`evals/contamination.py`](evals/contamination.py) drops results that reproduce
  SimpleQA or FinanceBench questions or answers.
- The checker never sees gold answers. Its cost and latency are always included in
  the totals.
- We compare against a single-engine agent and against Telem's default routing, with
  the same agent model, prompt and tools in every setup. Only the search tool differs.

Reproduce:

```sh
python -m evals.matrix            # hit matrix: every question on every engine via Telem
python -m analysis.rescue_table   # learn-split rescue order
python -m evals.run_eval && python -m evals.score
```

## Development

```sh
uv venv -p 3.13 .venv && uv pip install -p .venv -e ".[dev]"
.venv/bin/pytest -q
python -m so.second_opinion "What was Stripe's valuation in its 2023 round?" --goal "find the valuation figure" --format text
```

Every search writes a replayable JSONL event log through `so/eventlog.py`; see
[docs/api.md](docs/api.md#event-log) for the event types.

## Roadmap

- Rescue tables per domain (finance, biotech, legal), learned from customer traffic.
- Streaming responses so agents see the first engine's results while the check runs.
- Shared rate limiting and usage export for multi-worker hosted deployments.
- Answer-level verification: checking the agent's final claim against the cited page.
- SDKs for TypeScript and for common agent frameworks.

## License

MIT. See [LICENSE](LICENSE). Changes are listed in [CHANGELOG.md](CHANGELOG.md).
