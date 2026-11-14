# Deploy the race API

Run the backend as one persistent Python 3.11 process. The website can be hosted separately; the existing race section at `/#race` remains a reference client, and `/race` redirects there. `.python-version` retains the runtime pin.

```sh
uv venv --python 3.11
uv pip install .
SERVER_HOST=0.0.0.0 .venv/bin/python -m server
```

Set the provider credentials, models, prices and budgets through the environment as documented in `.env.example`. `SERVER_HOST` and `SERVER_PORT` control the listener. Do not commit credentials. Keep `SERVER_LIVE_RACES=false` until the deployment has its intended API keys and limits configured.

To enable live races, set `SERVER_LIVE_RACES=true` and `BACKSTOP_API_KEYS`. Set `SERVER_CORS_ORIGINS` to the separately hosted website's exact origin, or leave it empty for same-origin browser access only. [The integration guide](server/README.md) covers authentication, CORS, endpoints and EventSource usage.

Use one worker. It owns the active agent tasks, admission pool, request limiter and process budgets. Configure persistent writable locations for `SERVER_LOG_DIR`, `SO_CACHE_DIR` and `BACKSTOP_USAGE_DB`; defaults are documented in `.env.example`. Completed run logs and stored error metadata must be available to later stream requests. Restarting the worker ends in-flight agents; completed logs remain readable.

Terminate HTTPS at the hosting proxy and disable buffering for `/stream/` and `/replay/`. Keep streaming connections open for the configured run duration and allow SSE keepalives through. Ensure the process continues running after `POST /race` returns its run IDs.

## Vercel

The site and API deploy to Vercel as one Python function. `pyproject.toml` names the entrypoint (`[tool.vercel] entrypoint = "server.app:app"`) and `vercel.json` gives that function `maxDuration: 120`, which covers a race's 90 second limit plus start-up. Run logs go to `/tmp/backstop/logs` on Vercel unless `SERVER_LOG_DIR` is set.

On a serverless host the start-then-stream pair (`POST /race`, then `GET /stream/{run_id}`) can fail, because the stream request may reach an instance that never started the run. The race section therefore uses `POST /race/stream`, which starts both sides and streams both logs on the same response; closing it cancels both agents. The older routes still work on a persistent server.

Set these in the Vercel project (Production) to turn on public live races:

| Variable | Value |
| --- | --- |
| `BACKSTOP_LIVE_RACE` | `true` |
| `TELEM_API_KEY` | Telem key (every search and Telem fetch) |
| `TAVILY_API_KEY` | Tavily key (Tavily Extract page reads) |
| `ANTHROPIC_API_KEY` | Anthropic key (agent model, and the cascade's LLM checker stage) |
| `OPENAI_API_KEY` | OpenAI key (the cascade's first stage, the Decisions model) |
| `CHECKER_BACKEND` | `cascade` (the code default; set it so the deployment is explicit) |
| `FIRST_ENGINE_ROUTING` | `on` (the code default) |

Optional: `BACKSTOP_DEMO_CODE` to require a code, and `BACKSTOP_RACE_MAX_USD` (default 0.50), `BACKSTOP_RACE_TIMEOUT_S` (default 90), `BACKSTOP_MAX_RACES` (default 2) and `BACKSTOP_RACE_RATE_PER_HOUR` (default 10). `TELEM_BUDGET_USD` and `LLM_BUDGET_USD` cap spend per running instance (default 25 each). `GET /config` reports `live_available` and, when it is false, `live_reason` names what is missing; the race section shows that line.

On Vercel the per-race cost cap and time limit hold exactly, since a race lives inside one request. The concurrency and per-IP limits are kept in memory, so each instance enforces its own; the client IP comes from `x-vercel-forwarded-for`, which Vercel sets.

Mount or deploy the chosen recording directory and evaluation artifacts using `SERVER_DEMO_DIR`, `SERVER_RESULTS_PATH` and `SERVER_FRONTIER_PATH`. Missing artifacts are reported as unavailable. `SERVER_FIXTURE_DIR` stays empty in production.

After deployment, check `GET /health`, `GET /config` and `GET /replays`. Verify an allowed-origin preflight and an authenticated race using your deployment's budget guards. Public recordings remain available with live races disabled.
