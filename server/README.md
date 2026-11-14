# Race API integration

This backend supports a separately hosted race screen. The race section at `/#race` (`/race` redirects there) remains a reference client. There is no pitch presentation endpoint or combined `/race-stream` route. Search, checker and agent behavior is unchanged.

Run `python -m server` in the Python 3.11 environment after installing the project. Host, port, artifact paths, capacity and timeouts come from `so.config`. Use **one persistent worker** with writable, persistent log storage. See [deployment instructions](../DEPLOY.md).

## Authentication, limits and CORS

Live races are disabled by default (`SERVER_LIVE_RACES=false`); public recordings still work. To enable them, configure `SERVER_LIVE_RACES=true` and `BACKSTOP_API_KEYS`. Start requests require `Authorization: Bearer <key>`. Never put a key in a URL or ship an operator key in frontend assets. The reference client accepts a user-entered key and keeps it in page memory only.

`POST /race` uses `api_v1.require_key` through `rate_limited_key`, unchanged. It shares the per-key request limiter with `/v1/search` and admits two agents against the single `SERVER_MAX_ACTIVE_RUNS` pool. Disabled live requests return 403, missing/invalid bearer keys return 401, and rate or concurrency limits return 429 before agents start. Rate-limit responses include `Retry-After`. Admission and rate limits remain per process.

Set `SERVER_CORS_ORIGINS` to the website's exact origins, comma-separated:

```dotenv
SERVER_CORS_ORIGINS=https://site.example,http://localhost:3000
```

The default is empty, meaning same-origin browser access only. Origins include the scheme, hostname and optional port, with no path, trailing slash or wildcard. Restart the server after changing them. Allowed origins can make GET/POST requests and preflight `Authorization`, `Content-Type` and `Last-Event-ID` headers. Responses, including SSE and handled errors, carry the allowed origin. `Retry-After` and `WWW-Authenticate` are readable by the client. Cookies are not enabled; use `credentials: 'omit'` on fetch and the default EventSource credential setting. CORS does not replace API-key authentication.

Race spend stays in each run's event log. Every finished, failed or cancelled agent also emits one structured `race_usage` server log containing a hashed key ID and recorded spend, including partial failures. **Per-key race billing is not implemented**; `/v1/usage` reports verified-search usage only.

## Endpoints

| Endpoint | Auth | Response |
| --- | --- | --- |
| `POST /race` | Bearer key; live gate and shared limits | `{left_run_id, right_run_id}` |
| `POST /race/stream` | Same as `POST /race` | Both runs' SSE events on this response (serverless safe) |
| `GET /stream/{run_id}` | Public | One run's SSE events |
| `GET /replays` | Public | Array of `{name, title, question, fixture, left, right}` |
| `GET /replay/{name}` | Public | A recorded pair's interleaved SSE events |
| `GET /results` | Public | `{results, frontier}` from configured artifacts |
| `GET /config` | Public | `{first_engine, first_engine_routing, max_question_chars, live_available, live_reason, race_stream, ...}` |
| `GET /health` | Public | Service liveness |

`POST /race` takes a JSON body such as `{ "question": "Your question", "left": "single", "right": "second_opinion" }`. Both setup names must be one of `single`, `telem_default`, `second_opinion`. The defaults are `single` and `second_opinion`. Blank questions or those exceeding `SERVER_MAX_QUESTION_CHARS` return 422. Agent startup failures return 503. The response IDs come from the agents' actual logs.

Run streams are public to anyone with the run ID and require no bearer header, so native EventSource works. Disconnecting or closing a stream **does not cancel the agent**. It continues until completion, its configured timeout, or worker shutdown. A new POST starts new work; reconnect to the existing ID to continue viewing.

`GET /results` validates the configured results and frontier artifacts against the shared contracts and preserves their provenance fields. Missing artifacts are `null`; malformed artifacts return 503. Consumers must respect each artifact's counts, metric definitions and simulated/observed labels. The reference race client does not make evaluation claims.

## One-request races

`POST /race/stream` takes the same body, checks and guards as `POST /race`, starts both setups and streams both runs on the same response, so it works when each request may reach a different instance (Vercel). Messages: `race` (`{question, left, right, race_max_usd, race_timeout_s}`), then `log` (`{side, event}` with one unmodified section 6.11 event), then per side `complete` (`{side, run_id}`) or `stream_error` (`{side, run_id, error_type, message}`), and finally `done`. Idle periods send `: keep-alive` comments. Closing the response cancels both agents. EventSource cannot POST, so read it with `fetch` and a `ReadableStream`, as `ui/race-stream.js` does.

## SSE protocol

Every message uses standard SSE framing: `event: <kind>`, optional `id: <cursor>`, `data: <JSON>`, then a blank line. Responses use `Content-Type: text/event-stream`, `Cache-Control: no-cache` and `X-Accel-Buffering: no`. Live idle streams send `: keep-alive` comments. Disable proxy buffering.

For `/stream/{run_id}`:

- `log`: one unmodified [event-log event](../docs/api.md#event-log). Its SSE ID is the event's numeric `seq`. Native EventSource reconnects with `Last-Event-ID`; the server skips events at or below that sequence. An invalid cursor returns 400.
- `complete`: `{run_id}`. Close the EventSource.
- `stream_error`: `{message, run_id?, error_type?}`. Close the EventSource and show a failed state with the reason.

A log event has `{ts, run_id, question_id, setup, seq, type, data}`. `ts` is an ISO timestamp; `seq` increases within a run. Deduplicate using `(run_id, seq)`. The `data` fields below are the [event-log payloads](../docs/api.md#event-log); preserve additional fields in audit exports.

| Event type | Data |
| --- | --- |
| `run_start` | `question` |
| `search` | `query`, `goal`, `engines`, `n_results`, `latency_ms`, `cost_usd`, `cached` |
| `check` | `verdict`, `confidence`, `why`, `evidence_rank` |
| `rescue` | `action` (`RETRY_ENGINE` or `FETCH_PAGE`), `engine`, `query`, `reason` |
| `fetch` | `url`, `ok`, `via` (`tavily` or `telem`), `latency_ms`, `cost_usd` |
| `search_done` | `status`, `calls`, `fetches`, `cost_usd`, `latency_ms`, `note` |
| `llm` | `role`, `model`, `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms` |
| `answer` | `final`, `status`, `citations` |
| `run_end` | `engine_calls`, `fetches`, `searches`, `cost_usd_search`, `cost_usd_llm`, `cost_usd_total`, `latency_ms` |

Optional display fields:

- `search.data.first_engine` and `search.data.question_type`: the chosen engine and classified question type when recorded. Omit the corresponding display if absent; do not infer a classification.
- `check.data.best_url` and `check.data.evidence_title`: the cited source URL and title when known. `evidence_rank` is a one-based position in the checked result list, not the later reranked list. Link a HIT to `best_url`; use the URL as the label when no title is present. Never guess a URL from the final citations.
- `session_id` or `telem_session_id` may be present in event data. Preserve and display the recorded session identifier.

A rescue's `query` may differ from the immediately preceding search query. Display the old/new query together with the verdict and reason. A HIT can mark that source as checker-verified, but it does not verify the whole final answer.

`run_end` also occurs during failure cleanup. Wait for `complete` or `stream_error` to determine terminal success. A deliberate `answer.status: NOT_FOUND` is a completed outcome, distinct from a failed run. Failure metadata is persisted beside the log, so reconnecting after a server restart retains the reason. Completed logs remain readable after restart; interrupted agents do not resume.

Running totals should add search, fetch and agent/checker model costs once. Do not add `search_done` summary costs again. Use `run_end` totals when available. The Telem counter counts search requests plus fetches with `via: telem`; Tavily Extract page reads have separate attribution.

## Minimal browser integration

Supply `apiBase` from the website's deployment configuration and `apiKey` from the authorized caller. The following starts a race, then opens one native EventSource per run. `onEvent(side, event)` receives the original contract event and `onState(side, state, message)` updates the column state.

```js
async function startRace(apiBase, apiKey, question, onEvent, onState) {
  const response = await fetch(new URL('/race', apiBase), {
    method: 'POST', credentials: 'omit',
    headers: {'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}`},
    body: JSON.stringify({question, left: 'single', right: 'second_opinion'}),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail?.message || JSON.stringify(body.detail));
  const streams = ['left', 'right'].map(side => {
    const source = new EventSource(new URL(`/stream/${encodeURIComponent(body[`${side}_run_id`])}`, apiBase));
    source.addEventListener('log', message => onEvent(side, JSON.parse(message.data)));
    source.addEventListener('complete', () => {
      source.close(); onState(side, 'complete');
    });
    source.addEventListener('stream_error', message => {
      source.close(); onState(side, 'failed', JSON.parse(message.data).message);
    });
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) onState(side, 'failed', 'Stream unavailable');
      else onState(side, 'reconnecting'); // Native SSE resumes with Last-Event-ID.
    };
    return source;
  });
  return () => streams.forEach(source => source.close()); // Stops viewing, not the agents.
}
```

## Replay labels and fixtures

Fetch `/replays`, select a pair, then open `new EventSource(new URL('/replay/' + encodeURIComponent(pair.name), apiBase))`. The stream begins with a `replay` metadata event. Each `log` payload is `{side: 'left' | 'right', event: <event-log event>}`; its SSE ID is `<side>-<seq>`. `complete` contains `{replay: name}`. A replay starts at the beginning on each connection; `Last-Event-ID` resume applies only to live run streams. Replays preserve the original event timings and costs and make no provider calls.

Always label actual recordings **Replay of a live run** while playing. If the catalog or metadata says `fixture: true`, label it **Synthetic fixture · not a live run**. A recording is never a fresh live run or a new measured result. Missing recordings should be shown as unavailable, without substituting a fixture as product evidence.

Pairs live in `SERVER_DEMO_DIR` as a JSON manifest with relative log paths:

```json
{"title": "Recorded question", "left": "single.jsonl", "right": "second_opinion.jsonl"}
```

An optional integer `order` lists a pair first in `/replays`, lowest first; pairs without it follow in file name order. Each JSONL must contain one complete run, starting with `run_start` and ending with `run_end`. Paths must stay inside the recording directory. Malformed recordings return 503; an unknown pair returns 404. Synthetic manifests use `fixture: true`.

Fixtures are disabled by default. For local acceptance, set `SERVER_FIXTURE_DIR=tests/o4/fixtures` and start `python -m server`. `/replay/fixture-stripe` emits 25 events with a page-read HIT; `/replay/fixture-ranked` emits 21 events with a result-2 HIT. They are synthetic test data. The reference client's audit download contains the received event arrays without rewriting them.

```sh
SO_ENV_FILE=/dev/null python -m pytest tests/ -q
node --test tests/o4/*.mjs
```

The suite uses fake runners only under `tests/` and makes no paid calls. An offline browser failure preview is available through `uvicorn tests.o4.preview_failure:app` with an explicit test bearer key and live-race enablement.
