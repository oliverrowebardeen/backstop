# Race server

Run from the repository root after installing `.[dev]` in the Python 3.11 virtual environment:

```sh
python -m server
```

The host, port, artifact paths, question limit, active-run capacity, startup timeout, run timeout, polling interval, and SSE keepalive interval come from `so.config`. Use one server worker: it owns the background agent tasks and their concurrency limit. `python -m server` starts that configuration directly; `uvicorn server.app:app --port 8000` is also supported for local development.

Live races are disabled by default (`SERVER_LIVE_RACES=false`). Public visitors can still play recorded replays. To enable live races, set `SERVER_LIVE_RACES=true`, configure `BACKSTOP_API_KEYS`, and send `Authorization: Bearer <key>`. The browser shows the key input only when live races are enabled and keeps the entered key in page memory only.

Both `/race` and `/race-stream` use the unchanged `api_v1.require_key` and `rate_limited_key` dependencies, sharing the per-key request limit with `/v1/search`. A race request admits two agents against one shared `SERVER_MAX_ACTIVE_RUNS` pool across both race routes. Admission and rate limits are per process; run one worker and keep live races disabled if the deployment cannot enforce that boundary.

Race spend remains in each run's event log. Each finished, failed, or cancelled agent also emits one structured `race_usage` JSON message through the Uvicorn server logger with the hashed key ID, question ID, side, setup, failure flag, engine calls, page reads, and recorded cost. Summaries are not counted twice. **Per-key race billing is not implemented**; `/v1/usage` continues to report verified-search usage only.

For fixture acceptance, explicitly enable the test recordings:

```sh
SERVER_FIXTURE_DIR=tests/o4/fixtures python -m server
```

Request `/replay/fixture-stripe` to check the fixture directly. Both columns animate for 3.7 seconds. The left run ends at 1 engine call, $0.0080, and 1.8 seconds; the right ends at 2 engine calls, $0.0136, and 3.7 seconds. Each has 2 Telem calls. These are synthetic test events, not evaluation results. Fixtures are disabled by default.

```sh
pytest tests/o4 -q && node --test tests/o4/test_ui.mjs
```

For an offline failure check, enable live races, configure a local test bearer key, and run `uvicorn tests.o4.preview_failure:app`. Enter that key in the browser and submit any question. The test runner rejects both runs with a simulated provider usage limit. Both columns must show **Failed** with the reason; no external calls are made.

## Routes and event streams

`POST /race` accepts `{question, left, right}` using the three setup names in `so.contracts`. It starts `so.agent.run_question` twice and returns the agent's actual `left_run_id` and `right_run_id`. Each run writes to its own directory below `SERVER_LOG_DIR`. Missing agents, failed startup, timeouts, capacity limits, and incomplete logs produce explicit errors.

`POST /race-stream` accepts the same authenticated request and streams both columns in the response. Disconnecting cancels its agents and releases their shared capacity. Disabled live routes return 403, missing or invalid bearer keys return 401, and exhausted rate or concurrency limits return 429 before starting work.

`GET /stream/{run_id}` emits `log` SSE messages containing unmodified section 6.11 events. The SSE ID is the event sequence number; `Last-Event-ID` resumes after that sequence. `complete` ends a successful stream. `stream_error` ends an interrupted or malformed stream. Completed logs remain readable after a server restart. Browser disconnection does not cancel the agent.

The agent also writes `run_end` during failure cleanup. The server waits for the task outcome before sending a terminal stream message and reports failed cleanup as `stream_error`. Redacted failure metadata is stored beside the log in `<run_id>.error.json` so reconnects retain the reason. Server diagnostics include the exception type, message, and redacted traceback. The UI shows **Failed** and a plain explanation separately from a deliberate **Not found** outcome.

`GET /replays` lists complete recording pairs. `GET /replay/{name}` interleaves their logs using each run's original elapsed time. Its `log` messages contain `{side, event}`. It also emits `replay` metadata and a final `complete` message. A pair is a JSON manifest in `SERVER_DEMO_DIR`:

```json
{
  "title": "Recorded question",
  "left": "single.jsonl",
  "right": "second_opinion.jsonl"
}
```

The two JSONL paths are relative to that directory. Each must contain one complete run, starting with `run_start` and ending with `run_end`. Incomplete or malformed recordings are reported as errors. Other JSON files without pair fields are ignored. Test manifests use `fixture: true`; the UI labels them as synthetic. Actual recordings are labeled **Replay of a live run**.

`GET /results` returns `{results, frontier}` from the configured artifact paths, validated with the shared contracts. An unpublished artifact is `null`; invalid artifacts return a service error. The UI shows evaluation question and trap counts separately. Frontier coverage is labeled **Answer found in results** and is distinct from answer accuracy.

`GET /config` returns the first engine, question length limit, and live availability flag. `/presentation` also exposes live availability for the page. Credentials and private configuration are never sent to the browser. The UI's audit button downloads the received events, including search queries, checks, page reads, citations, and final totals.

The engine counter counts engines in search events. The Telem counter counts search requests plus page reads with `via: telem`; Tavily Extract reads have their own visible attribution. Running cost includes search, fetch, and agent/checker model events without adding `search_done` summaries again. Final counters use the `run_end` totals.
