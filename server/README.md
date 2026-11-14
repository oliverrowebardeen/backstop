# Race server

Run from the repository root after installing `.[dev]` in the Python 3.11 virtual environment:

```sh
python -m server
```

The host, port, artifact paths, question limit, active-run capacity, startup timeout, run timeout, polling interval, and SSE keepalive interval come from `so.config`. Use one server worker: it owns the background agent tasks and their concurrency limit. `python -m server` starts that configuration directly; `uvicorn server.app:app --port 8000` is also supported for local development.

For fixture acceptance, explicitly enable the test recordings:

```sh
SERVER_FIXTURE_DIR=tests/o4/fixtures python -m server
```

Open the root page, select the fixture, and choose **Play replay**. Both columns animate for 3.7 seconds. The left run ends at 1 engine call, $0.0080, and 1.8 seconds; the right ends at 2 engine calls, $0.0136, and 3.7 seconds. Each has 2 Telem calls. These are synthetic test events, not evaluation results. Fixtures are disabled by default.

```sh
pytest tests/o4 -q && node --test tests/o4/test_ui.mjs
```

## Routes and event streams

`POST /race` accepts `{question, left, right}` using the three setup names in `so.contracts`. It starts `so.agent.run_question` twice and returns the agent's actual `left_run_id` and `right_run_id`. Each run writes to its own directory below `SERVER_LOG_DIR`. Missing agents, failed startup, timeouts, capacity limits, and incomplete logs produce explicit errors.

`GET /stream/{run_id}` emits `log` SSE messages containing unmodified section 6.11 events. The SSE ID is the event sequence number; `Last-Event-ID` resumes after that sequence. `complete` ends a successful stream. `stream_error` ends an interrupted or malformed stream. Completed logs remain readable after a server restart. Browser disconnection does not cancel the agent.

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

`GET /config` returns only the first engine and the question length limit. Credentials and private configuration are never sent to the browser. The UI's audit button downloads the exact received events as a JSON array, including search queries, checks, page reads, citations, and final totals.

The engine counter counts engines in search events. The Telem counter counts search requests plus page reads with `via: telem`; Tavily Extract reads have their own visible attribution. Running cost includes search, fetch, and agent/checker model events without adding `search_done` summaries again. Final counters use the `run_end` totals.
