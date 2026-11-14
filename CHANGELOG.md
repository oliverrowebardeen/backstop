# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Optional `evidence` (the verified source, snippet and agreeing engines behind a
  `HIT`) and `ranking` fields on `BackstopSearchResponse` and `POST /v1/search`.

## [0.1.0] - 2026-11-14

First public release.

### Added

- `BackstopTelem`, a drop-in replacement for `telem.AsyncTelem` whose `search`
  verifies results before returning them (`from backstop import BackstopTelem`).
- Second Opinion loop: one pinned engine through Telem, a checker that labels the
  results `HIT`, `STALE`, `WRONG_ENTITY`, `SHALLOW` or `DEAD`, fixed rescue actions,
  and an explicit `NOT_FOUND` after two failed rescues.
- Rescue order learned from a hit matrix on the learn split (`out/rescue_table.json`).
- Ranked results: checker evidence first, then cross-engine agreement, then engine
  rank, with a `rank_reason` on every result.
- Visible query rewriting: rescues record `original_query` and `rewritten_query`.
- Per-call limits with config defaults: `max_rescues=2`, `max_cost_usd=0.03`,
  `timeout_s=12`, `checker_timeout_s=4`.
- Fail-open checking: a checker error or timeout returns the gathered results with
  status `UNCHECKED` and a note saying why.
- Engine errors are recorded and skipped without spending a rescue; a typed
  `TelemUnavailable` is raised only when Telem returns nothing usable.
- Hosted API: `POST /v1/search`, `GET /v1/usage`, `GET /health`, with bearer keys,
  per-key SQLite usage metering and a 60 requests per minute rate limit.
- Race screen and replayable JSONL event logs for every search.
- Evaluation harness: hit matrix, judge, contamination filter, eval runner and scoring.
