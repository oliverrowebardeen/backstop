# API reference

Backstop exposes the same verified search three ways: a Python client that replaces
`telem.AsyncTelem`, the lower-level `so.second_opinion.search` function, and a hosted
HTTP API.

- [Python client: `BackstopTelem`](#python-client-backstoptelem)
- [Limits](#limits)
- [Response](#response)
- [Errors](#errors)
- [Hosted HTTP API](#hosted-http-api)
- [Lower-level function](#lower-level-function-sosecond_opinionsearch)
- [Event log](#event-log)

## Python client: `BackstopTelem`

```python
from backstop import BackstopTelem

async with BackstopTelem() as client:
    resp = await client.search(
        "What was Stripe's valuation in its 2023 round?",
        goal="find the valuation figure",
    )
    if resp.status == "HIT":
        print(resp.results[0].url, resp.results[0].rank_reason)   # evidence first
    elif resp.status == "NOT_FOUND":
        print(resp.note)   # tell the agent not to guess
```

### Constructor

```python
BackstopTelem(
    api_key: str | None = None,
    *,
    telem: AsyncTelem | None = None,
    max_rescues: int | None = None,
    max_cost_usd: float | None = None,
    timeout_s: float | None = None,
    checker_timeout_s: float | None = None,
    log_dir: str | Path | None = None,
    **telem_options,
)
```

| Argument | Meaning |
|---|---|
| `api_key` | Telem key. Falls back to `TELEM_API_KEY`, exactly like `AsyncTelem`. |
| `telem` | Wrap an existing `AsyncTelem`-compatible client instead of building one. Cannot be combined with `api_key` or `telem_options`. |
| `max_rescues`, `max_cost_usd`, `timeout_s`, `checker_timeout_s` | Defaults for every search from this client. See [Limits](#limits). |
| `log_dir` | When set, each search writes a JSONL event log there. The race screen can replay these logs. |
| `**telem_options` | Passed to the `AsyncTelem` constructor (`base_url`, `timeout`, `default_tier`, ...). `base_url` defaults to `TELEM_BASE_URL`. |

Every attribute the client does not define (`fetch`, `providers`, `sessions`, ...) is
forwarded to the wrapped Telem client, so existing code keeps working. `client.telem`
returns the wrapped client for calls that should skip verification.

### `search`

```python
async def search(
    query: str,
    *,
    goal: str | None = None,
    session: str | None = None,
    max_rescues: int | None = None,
    max_cost_usd: float | None = None,
    timeout_s: float | None = None,
    checker_timeout_s: float | None = None,
    **telem_kwargs,
) -> BackstopSearchResponse
```

- `goal` is what the checker verifies against. Pass the agent's actual need, for
  example "find the valuation figure". It defaults to the query.
- `session` groups every Telem call in this search under one Telem session. If you
  omit it, the session Telem creates on the first call is reused for the rescues and
  returned as `resp.session_id`, so you can pass it to the next search.
- Any other keyword (`include_domains`, `topic`, `tier`, `published_before`, ...) is
  forwarded to every Telem call.
- The loop owns engine choice and freshness, so `providers_include`,
  `providers_exclude`, `auto_routing`, `max_routing_providers`, `num_results` and
  `published_after` are accepted for compatibility, logged as a warning and not
  forwarded.
- Batched queries (a list) raise `TypeError`. Call `search` once per query.

## Limits

Every search is bounded. Defaults live in `so/config.py` and can be set through the
environment, on the client constructor, or per call. The most specific setting wins.

| Limit | Default | Environment | Effect when reached |
|---|---|---|---|
| `max_rescues` | 2 | `MAX_RESCUES` | Outcome is `NOT_FOUND` after the first search plus this many rescues. |
| `max_cost_usd` | 0.03 | `SEARCH_MAX_COST_USD` | A rescue whose estimated cost (engine price from `PRICE_USD` plus the average checker cost so far) would push spend over the limit is not started. Outcome is `NOT_FOUND` and the note names the limit. |
| `timeout_s` | 12 | `SEARCH_TIMEOUT_S` | A rescue that is expected to finish after this many seconds of wall clock (estimated from this call's average step time) is not started. Outcome is `NOT_FOUND` and the note names the limit. |
| `checker_timeout_s` | 4 | `CHECKER_TIMEOUT_S` | A checker call slower than this is abandoned. Outcome is `UNCHECKED` with the results gathered so far. |

When no limit is reached, behavior is identical to the unbounded loop.

## Response

`BackstopSearchResponse` has the shape of `telem.SearchResponse`, plus the verification fields.

| Field | Type | Meaning |
|---|---|---|
| `results` | `list[BackstopResult]` | Merged, URL-deduplicated, ranked results. |
| `by_provider` | `list[BackstopProviderRun]` | One entry per engine call, in call order. |
| `session_id` | `str \| None` | Telem session used for every call. |
| `status` | `"HIT" \| "NOT_FOUND" \| "UNCHECKED"` | The outcome. This replaces Telem's interaction status. |
| `verdict` | `str \| None` | Last checker label: `HIT`, `STALE`, `WRONG_ENTITY`, `SHALLOW`, `DEAD`. `None` when unchecked. |
| `why` | `str` | The checker's reason, or why the check was skipped. |
| `note` | `str` | The instruction for the agent, for example "Answer is in result 1. You can stop searching for this." |
| `engines_called` | `list[str]` | Engines in call order. |
| `engine_calls`, `fetches` | `int` | Billable Telem engine queries and page reads. |
| `cost_usd` | `float` | Engines, page reads and checker calls, all included. |
| `latency_ms` | `float` | Wall clock for the whole search. |
| `trace` | `list[dict]` | Every event of this search (see [Event log](#event-log)). |
| `found` | property | `True` when `status == "HIT"`. |

`BackstopResult`: `url`, `title`, `summary`, `excerpt`, `publish_date`, `rank`,
`provider`, plus `rank_reason` and `engines` (all engines that returned this URL).
`result.content` returns the best available text, as on `telem.SearchResult`.

Ranking is: the result the checker cited as evidence (`rank_reason="evidence"`), then
results returned by more than one engine, more engines first (`"agreement: 2 engines"`),
then the rest by their best original engine rank (`"engine rank 3"`).

`BackstopProviderRun`: `provider`, `status`, `results`, `error`, `latency_ms`, `query`,
`batch_index`, as on `telem.ProviderRun`.

### Status

| Status | Meaning | What the agent should do |
|---|---|---|
| `HIT` | The checker found the answer in the results. | Answer from `results[0]` and cite it. Stop searching. |
| `NOT_FOUND` | No engine surfaced it after the allowed rescues, or a limit stopped the rescues. | Answer from other evidence, or answer NOT FOUND. Do not guess. |
| `UNCHECKED` | The checker failed or timed out. | Treat the results as unverified search output. |

### Verdicts and rescue actions

| Verdict | Meaning | Action |
|---|---|---|
| `HIT` | A top-5 title or snippet states the answer | Return results and tell the agent to stop |
| `SHALLOW` | The right page is in the results, but the snippet lacks the answer | Read the page (Tavily Extract, Telem fetch as fallback) and check again |
| `STALE` | On topic, but older than the goal needs | Retry on the next engine in the rescue order with `published_after` and the checker's rewrite |
| `WRONG_ENTITY` | Results are about a similarly named entity | Retry on the next engine with the checker's rewrite |
| `DEAD` | Nothing relevant | Retry once on the next engine with the checker's rewrite |

An engine is never retried within one search. An engine that errors is recorded and
skipped without spending a rescue.

## Errors

| Exception | When |
|---|---|
| `backstop.TelemUnavailable` | Telem returned nothing usable: the first call failed outright, or every engine errored. Subclass of `backstop.SearchError` and `RuntimeError`. |
| `so.config.BudgetExceeded` | The process-wide spend guard (`TELEM_BUDGET_USD`, `LLM_BUDGET_USD`) is exhausted. This is an operator stop and is never swallowed. |
| `TypeError`, `ValueError` | Invalid arguments: a batch query, an empty query, unknown or invalid limits. |

Checker failures never raise. They produce `status="UNCHECKED"`.

## Hosted HTTP API

Start the server (it also serves the race screen):

```sh
echo "BACKSTOP_API_KEYS=bk_live_one,bk_live_two" >> .env
uvicorn server.app:app --port 8000
```

### `POST /v1/search`

```sh
curl -s http://localhost:8000/v1/search \
  -H "Authorization: Bearer bk_live_one" \
  -H "Content-Type: application/json" \
  -d '{"query": "What was Stripe'\''s valuation in its 2023 round?",
       "goal": "find the valuation figure",
       "max_rescues": 2}'
```

Body fields: `query` (required, 1 to 2000 characters), `goal`, `session`,
`max_rescues` (0 to 5), `max_cost_usd` (up to 1), `timeout_s` (up to 120),
`checker_timeout_s` (up to 60). Unknown fields are rejected. The response is the
`BackstopSearchResponse` above as JSON.

### `GET /v1/usage`

Totals for the calling key:

```json
{"key_id": "9724786b5d45c739", "verified_searches": 1, "hits": 1, "not_found": 0,
 "unchecked": 0, "engine_calls": 1, "fetches": 0, "cost_usd": 0.00555,
 "first_seen": "2026-11-14T19:39:37+00:00", "last_seen": "2026-11-14T19:39:37+00:00"}
```

### `GET /health`

`{"status": "ok", "version": "0.1.0"}`. Needs no key.

### HTTP errors

Errors are JSON: `{"detail": {"code": "...", "message": "..."}}`. A 422 uses FastAPI's
standard validation list in `detail`.

| Status | Code | When |
|---|---|---|
| 401 | `unauthorized` | Missing, malformed or unknown bearer key |
| 422 | | Invalid body |
| 429 | `rate_limited` | More than `BACKSTOP_RATE_LIMIT_PER_MIN` (default 60) requests in a rolling minute for this key. `Retry-After` is set. |
| 502 | `telem_unavailable` | Telem returned nothing usable from any engine |
| 503 | `budget_exhausted` | The deployment's spend budget is exhausted |

### Operations

| Setting | Default | Meaning |
|---|---|---|
| `BACKSTOP_API_KEYS` | empty (all requests rejected) | Comma-separated accepted keys |
| `BACKSTOP_USAGE_DB` | `out/usage.db` | SQLite usage database |
| `BACKSTOP_RATE_LIMIT_PER_MIN` | 60 | Requests per key per rolling minute |

Keys are compared in constant time and never stored or logged. Usage rows and log
lines identify a key by `key_id`, the first 16 hex characters of its SHA-256. The rate
limiter is in memory and per process, so run one worker or put a shared limiter in
front of several.

## Lower-level function: `so.second_opinion.search`

```python
async def search(query: str, goal: str, ctx: RunContext, *,
                 limits: SearchLimits | None = None,
                 engine_search: EngineSearch | None = None) -> SearchOutcome
```

This is the search tool the evaluation agent calls. It returns the `SearchOutcome`
contract from `so/contracts.py`, with results in merge order. The ranked view is in the
trace's `search_done` event (`data["ranking"]`), and `ranked_results(outcome)` turns it
into `RankedResult` objects. `engine_search` replaces `so.telem_client.search_engines`,
which adds disk caching and the process budget.

Command line:

```sh
python -m so.second_opinion "What was Stripe's valuation in its 2023 round?" \
  --goal "find the valuation figure" --format text
```

`--format json` (the default) prints the outcome plus `ranked_results`. The
`--max-rescues`, `--max-cost-usd`, `--timeout-s` and `--checker-timeout-s` flags
override the limits.

## Event log

Each search emits these events, in order, into `trace` and, when a log is attached,
into `out/logs/<run_id>.jsonl`:

| Event | Key data |
|---|---|
| `search` | `query`, `goal`, `engines`, `n_results`, `latency_ms`, `cost_usd`, `cached`, `status`, `error` when an engine failed |
| `llm` | `role="checker"`, `model`, `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms` |
| `check` | `verdict`, `confidence`, `why`, `evidence_rank`, `best_url`, `rewrite` |
| `rescue` | `action` (`RETRY_ENGINE`, `FETCH_PAGE`), `engine`, `query`, `reason`, `counted`, `estimated_cost_usd`, `original_query` and `rewritten_query` when the checker's rewrite is used |
| `fetch` | `url`, `ok`, `via` (`tavily` or `telem`), `latency_ms`, `cost_usd` |
| `search_done` | `status`, `calls`, `fetches`, `cost_usd`, `latency_ms`, `note`, `ranking`, plus `stopped_by` or `check_skipped` when a limit or checker failure ended the search, and `evidence` on every HIT |

On the SHALLOW path the page is read with Tavily Extract (basic depth, the goal as its
query, `TAVILY_EXTRACT_CHUNKS` chunks of at most 500 characters, `TAVILY_EXTRACT_TIMEOUT_S`
timeout, priced at `TAVILY_EXTRACT_PRICE_USD`); a failed or empty Extract falls back to
the Telem fetch. The HIT `evidence` object makes no extra calls:

| Field | Meaning |
|---|---|
| `verified` | always `true` (the checker returned HIT) |
| `snippet` | the chunk, page text or result snippet that states the answer, at most 500 characters |
| `url` | the evidence URL |
| `engines_agreeing`, `agreeing_engines` | engines whose already-fetched top results contain the answer the checker quoted (string or normalized number) |
| `rank_reason` | the evidence URL's ranking reason without the evidence boost |
| `source`, `chunks` | `search`, `tavily_extract` (with the chunk count) or `telem_fetch` |
