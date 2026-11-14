# Backstop pitch and live race

The single page at `/` replaces the earlier race/results screen. It has seven beats, in pitch order: hook, first-search misses, moving query diagram, live race, measured proof, infrastructure and audit, business. The frontend is plain HTML/CSS/JavaScript in `ui/`; headings use the self-hosted, OFL-licensed Libre Caslon Display font.

## Run locally

From the repository root, with the existing Python environment and `.env`:

```sh
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

`python -m server` also serves the same page. Use one worker for local development.

## Deploy to Vercel

Import `oliverrowebardeen/backstop`, select the repository root (`.`), and use the **FastAPI** framework preset. No frontend build command or output directory is required. `app.py` is the Vercel entry point; `vercel.json` sets a 300-second function limit. The race has its own shorter configured timeout.

Add the existing runtime values as Vercel environment variables: `TELEM_API_KEY`, `TAVILY_API_KEY`, `LLM_PROVIDER`, the chosen model provider's API key, `CHECKER_MODEL`, `AGENT_MODEL`, `JUDGE_MODEL`, and `TELEM_BASE_URL`. Use the team's actual Telem endpoint. Keep credentials out of source. Existing price overrides and model settings continue to work through `so.config`.

Vercel runtime cache and event logs use `/tmp/backstop/`; evaluation artifacts stay in the deployed `out/`. `POST /race-stream` runs and streams both agents within one request, so the presentation does not depend on another function instance finding an in-memory job. Disconnection cancels both jobs. Provider budgets remain the shared process-level guards, not a deployment-wide spending ledger.

The separately authenticated `/v1` API is preserved. Its SQLite usage database also defaults to `/tmp/backstop/` on Vercel and is ephemeral; use persistent hosting for durable usage metering.

Reference: [Vercel FastAPI support](https://vercel.com/docs/frameworks/backend/fastapi).

## Numbers and charts

- `/presentation` reads the latest files on each request; the browser refreshes every 30 seconds while visible.
- Beat 2 joins `out/matrix.jsonl` to the non-trap test IDs in `data/questions.jsonl`, selecting the configured first engine. It shows observed misses with their denominator. This is retrieval coverage, not final-answer accuracy.
- Beat 5 reads `out/results.json`. All three setups must have complete counts for `n_test`, valid costs, and rates consistent with their counts. Missing, partial, or explicitly synthetic data stays blank.
- Wrong-answer reduction compares Backstop with `single`. Accuracy and total cost compare it with `telem_default`. Labels change if Backstop performs worse. A tie means equal observed accuracy, not statistical equivalence.
- The chart uses final-answer accuracy and total cost from the same evaluation for all three dots. Its line connects only nondominated measured points. It does not force Backstop onto the frontier or mix retrieval coverage with answer accuracy. `out/frontier.json` remains available through the existing `/results` API and analysis links; its current simulated point is not used as a measured claim.
- Updating artifacts in a Vercel deployment requires redeploying the commit containing those artifacts. Local changes are picked up on the next refresh.

## Golden recordings and live input

The page autoplays one real recorded pair once when beat 4 enters view. The visible label says **Replay of a live run**; no paid request is started by scrolling. New questions use the **Run live** button. Recordings preserve original timings, costs, engine provenance, verdicts, and citations. The audit download contains both unmodified received event arrays and the playback mode.

`demo/golden-tavily.json` is an unmodified genuine paired race from the O4 server. Both agents answered that question correctly; it is not evidence of an accuracy or cost improvement. Replace it with a more representative completed pair when the evaluation identifies one. Fixture replays are never automatically played and are always labeled synthetic.

To record a new Stripe pair using the current runtime configuration:

```sh
python -m scripts.record_golden
```

Real calls use the configured budget guards. Incomplete attempts never become replay manifests. `server/README.md` describes the manifest and event format.

## Verify

```sh
python -m pytest tests/site tests/o4 -q
node --test tests/site/test_proof.mjs tests/o4/test_ui.mjs
```

These cover complete and interrupted streamed races, exact audit preservation, held-out miss denominators, unfinished/synthetic result suppression, truthful metric direction, and the measured frontier. No paid model calls are used by tests.
