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

The pitch's Vercel entry point, configuration and ignore file were removed with its request-scoped `/race-stream` route. The retained start-then-stream API requires a persistent process and shared log storage between requests; this repository no longer supplies a Vercel deployment configuration.

Mount or deploy the chosen recording directory and evaluation artifacts using `SERVER_DEMO_DIR`, `SERVER_RESULTS_PATH` and `SERVER_FRONTIER_PATH`. Missing artifacts are reported as unavailable. `SERVER_FIXTURE_DIR` stays empty in production.

After deployment, check `GET /health`, `GET /config` and `GET /replays`. Verify an allowed-origin preflight and an authenticated race using your deployment's budget guards. Public recordings remain available with live races disabled.
