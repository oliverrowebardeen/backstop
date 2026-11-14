"""Memo Check: split a memo into atomic factual claims and verify each through Backstop on Telem.

Pipeline (one memo, one Telem session, one `memo-` event log):

1. `extract_claims`: one CHECKER_MODEL call returns at most 12 atomic claims,
   each with the exact memo span that states it.
2. First pass: every claim's query in ONE batched Telem search on the first
   engine (`so.telem_batch.search_batch`), mapped back by batch_index.
3. `verify_claim` on the first-pass results. A claim still undecided gets, in
   order and while budget and time remain: a targeted query restricted to
   authoritative domains (numbers and events only), the Backstop cascade
   (`so.second_opinion.search`), and a Tavily Extract page read re-verified on
   the page text. Anything still not VERIFIED or CONTRADICTED is NOT_FOUND.

Limits (defined here, not in so.config): 12 claims, 4,000 characters, $0.40 and
90 seconds per memo, 6 claims verified at once. Every failure is per claim: an
error marks only that claim NOT_FOUND and records the error in its trace.
Evidence is never invented: source URLs come only from the results shown, and a
CONTRADICTED verdict without a source URL and a quote becomes NOT_FOUND.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, Field, PrivateAttr

from so import config, llm, second_opinion, tavily_client, telem_batch
from so.contracts import Result, RunContext
from so.eventlog import EventLog
from so.ledger import BACKSTOP_VERSION

MAX_CLAIMS = 12
MAX_MEMO_CHARS = 4000
MAX_COST_USD = 0.40
TIMEOUT_S = 90.0
CONCURRENCY = 6
NUM_RESULTS = 5
EXTRACT_MAX_TOKENS = 2000
EVIDENCE_CHARS = 300
CONTEXT = "backstop memo check"
BATCH_GOAL = "Find sources that confirm or refute each factual claim in a company memo"
# Rough per-step reservations against the memo cap (the real cost is what the calls record).
LLM_STEP_ESTIMATE_USD = 0.003
AUTHORITATIVE_DOMAINS = ["sec.gov", "reuters.com", "bloomberg.com", "cnbc.com", "wsj.com", "ft.com",
                         "apnews.com", "businesswire.com", "prnewswire.com"]

PROMPTS = Path(__file__).resolve().parent / "prompts"
EXTRACT_PROMPT = (PROMPTS / "claim_extract.md").read_text(encoding="utf-8")
VERIFY_PROMPT = (PROMPTS / "claim_verify.md").read_text(encoding="utf-8")

Kind = Literal["number", "date", "entity_role", "event", "other"]
StepVerdict = Literal["VERIFIED", "CONTRADICTED", "INSUFFICIENT", "IRRELEVANT"]
Verdict = Literal["VERIFIED", "CONTRADICTED", "NOT_FOUND"]
DECISIVE = ("VERIFIED", "CONTRADICTED")


class MemoError(ValueError):
    """The memo cannot be checked (empty or too long)."""


class CostCapReached(RuntimeError):
    """The next step would take the memo past its cost cap."""


class TimeLimitReached(RuntimeError):
    """Too little of the memo's time limit is left to start the next step."""


class Claim(BaseModel):
    id: str
    text: str
    kind: Kind = "other"
    entities: list[str] = Field(default_factory=list)
    goal: str
    query: str
    quote: str = ""
    # Character span [start, end) of the quote in the original memo, None if it could not be located.
    span: tuple[int, int] | None = None


class ClaimCheck(BaseModel):
    """One verify_claim result on one set of sources."""

    verdict: StepVerdict
    confidence: float = 0.0
    why: str = ""
    evidence_snippet: str = ""
    source_url: str | None = None
    evidence_rank: int | None = None
    cost_usd: float = 0.0
    note: str | None = None
    _usage: Any = PrivateAttr(default=None)


class ClaimResult(Claim):
    verdict: Verdict = "NOT_FOUND"
    confidence: float = 0.0
    why: str = "Not checked."
    evidence_snippet: str = ""
    source_url: str | None = None
    engines_used: list[str] = Field(default_factory=list)
    rescues_used: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)


class MemoTotals(BaseModel):
    verified: int = 0
    contradicted: int = 0
    not_found: int = 0


class MemoReport(BaseModel):
    run_id: str
    text: str
    checked_at: str
    claims: list[ClaimResult] = Field(default_factory=list)
    totals: MemoTotals = Field(default_factory=MemoTotals)
    engine_calls: int = 0
    fetches: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    telem_session_id: str | None = None
    telem_interaction_id: str | None = None
    limits: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)


# Claim extraction needs more output than the checker's 399-token cap. The override is
# scoped to this task by a ContextVar, so every other checker call keeps its exact budget.
_MAX_TOKENS_OVERRIDE: ContextVar[int | None] = ContextVar("memo_extract_max_tokens", default=None)


class _RoleMaxTokens(dict):
    def __getitem__(self, role: str) -> Any:
        override = _MAX_TOKENS_OVERRIDE.get()
        if override is not None and role == "checker":
            return override
        return dict.__getitem__(self, role)


def _install_token_override() -> None:
    if not isinstance(config.LLM_MAX_TOKENS, _RoleMaxTokens):
        config.LLM_MAX_TOKENS = _RoleMaxTokens(config.LLM_MAX_TOKENS)


# Parsing helpers

def _json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model reply is not JSON") from None
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("model reply is not a JSON object")
    return value


def _squash(text: str) -> str:
    return " ".join(str(text or "").split())


def locate(text: str, quote: str, taken: list[tuple[int, int]] | None = None) -> tuple[int, int] | None:
    """Find quote in text: exact, else case and whitespace insensitive; prefer a span not already taken."""
    quote = (quote or "").strip()
    if not quote:
        return None
    words = [re.escape(word) for word in quote.split()]
    pattern = re.compile(r"\s+".join(words), re.IGNORECASE)
    matches = [(m.start(), m.end()) for m in pattern.finditer(text)]
    if not matches:
        return None
    taken = taken or []
    for span in matches:
        if all(span[1] <= a or span[0] >= b for a, b in taken):
            return span
    return matches[0]


_KINDS = {"number", "date", "entity_role", "event", "other"}


def parse_claims(reply: str, text: str) -> list[Claim]:
    """Turn the extraction reply into at most MAX_CLAIMS claims with memo spans."""
    rows = _json(reply).get("claims") or []
    claims: list[Claim] = []
    taken: list[tuple[int, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        claim_text = _squash(row.get("text"))
        if not claim_text:
            continue
        quote = str(row.get("quote") or "").strip()
        span = locate(text, quote, taken)
        if span:
            taken.append(span)
        kind = str(row.get("kind") or "other").strip().lower()
        entities = [_squash(e) for e in (row.get("entities") or []) if isinstance(e, str) and e.strip()]
        claims.append(Claim(
            id=f"c{len(claims) + 1}", text=claim_text[:400], kind=kind if kind in _KINDS else "other",
            entities=entities[:6], goal=_squash(row.get("goal"))[:300] or f"a source stating: {claim_text}"[:300],
            query=_squash(row.get("query"))[:200] or claim_text[:200], quote=quote[:400], span=span))
        if len(claims) == MAX_CLAIMS:
            break
    return claims


_CLAIM_SCHEMA = {
    "type": "object",
    "properties": {"claims": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "text": {"type": "string"}, "quote": {"type": "string"},
            "kind": {"type": "string", "enum": sorted(_KINDS)},
            "entities": {"type": "array", "items": {"type": "string"}},
            "query": {"type": "string"}, "goal": {"type": "string"},
        },
    }}},
}

_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["VERIFIED", "CONTRADICTED", "INSUFFICIENT", "IRRELEVANT"]},
        "confidence": {"type": "number"},
        "why": {"type": "string"},
        "evidence_rank": {"type": ["integer", "null"]},
        "evidence_quote": {"type": "string"},
    },
}


async def _extract(text: str) -> tuple[list[Claim], Any]:
    _install_token_override()
    token = _MAX_TOKENS_OVERRIDE.set(EXTRACT_MAX_TOKENS)
    try:
        response = await llm.complete("checker", EXTRACT_PROMPT, [{"role": "user", "content": f"MEMO:\n{text}"}],
                                      json_schema=_CLAIM_SCHEMA)
    finally:
        _MAX_TOKENS_OVERRIDE.reset(token)
    return parse_claims(response.text, text), response


async def extract_claims(text: str) -> list[Claim]:
    """At most MAX_CLAIMS atomic, checkable claims from the memo (one CHECKER_MODEL call)."""
    claims, _ = await _extract(validate_memo(text))
    return claims


def validate_memo(text: str | None) -> str:
    text = (text or "").strip()
    if not text:
        raise MemoError("Paste a memo to check.")
    if len(text) > MAX_MEMO_CHARS:
        raise MemoError(f"Memos are limited to {MAX_MEMO_CHARS:,} characters.")
    return text


# Verification

@dataclass(frozen=True)
class Source:
    title: str
    url: str
    text: str
    published: str | None = None


def _sources(results: list[Result]) -> list[Source]:
    seen: set[str] = set()
    sources = []
    for result in results:
        if result.url in seen:
            continue
        seen.add(result.url)
        sources.append(Source(title=result.title, url=result.url, text=result.snippet, published=result.published))
    return sources[:10]


def render_sources(sources: list[Source], *, page: bool = False) -> str:
    limit = config.PAGE_TEXT_CHARS if page else 600
    return "\n".join(f"[{i}] {_squash(s.title)[:200] or '(untitled)'}\n    url: {s.url}\n"
                     f"    published: {s.published or 'unknown'}\n    text: {_squash(s.text)[:limit] or '(empty)'}"
                     for i, s in enumerate(sources, 1))


def _verbatim(quote: str, source: Source) -> bool:
    needle = _squash(quote).lower().strip(" .\"'")
    return bool(needle) and needle in _squash(f"{source.title} {source.text}").lower()


def parse_check(reply: str, sources: list[Source]) -> ClaimCheck:
    """Validate a verify reply against the sources it saw. Never invents a URL or a quote."""
    value = _json(reply)
    verdict = str(value.get("verdict") or "").strip().upper()
    if verdict not in {"VERIFIED", "CONTRADICTED", "INSUFFICIENT", "IRRELEVANT"}:
        verdict = "INSUFFICIENT"
    try:
        confidence = min(1.0, max(0.0, float(value.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    why = _squash(value.get("why"))[:300]
    rank = value.get("evidence_rank")
    source = sources[rank - 1] if isinstance(rank, int) and 1 <= rank <= len(sources) else None
    quote = _squash(value.get("evidence_quote"))
    note = None
    snippet = ""
    if source is not None and verdict in DECISIVE:
        if quote and _verbatim(quote, source):
            snippet = quote
        elif source.text.strip():
            # The model's quote is not in the source: show the source's own text instead.
            snippet, note = _squash(source.text), "quote not verbatim; showing the source text"
    snippet = snippet[:EVIDENCE_CHARS]
    check = ClaimCheck(verdict=verdict, confidence=confidence, why=why, evidence_snippet=snippet,
                       source_url=source.url if source else None, evidence_rank=rank if source else None, note=note)
    if verdict in DECISIVE and (check.source_url is None or not check.evidence_snippet):
        missing = "source URL" if check.source_url is None else "quoted snippet"
        return check.model_copy(update={
            "verdict": "INSUFFICIENT", "evidence_snippet": "", "source_url": None, "evidence_rank": None,
            "note": f"{verdict} without a {missing}; not accepted", "why": why})
    return check


async def verify_claim(claim: Claim, results: list[Result], today: date | str | None = None, *,
                       page: Source | None = None) -> ClaimCheck:
    """Judge one claim against results (or one page's text). Sees only claim, goal, sources and today."""
    sources = [page] if page is not None else _sources(results)
    if not sources:
        return ClaimCheck(verdict="IRRELEVANT", why="No sources to check.")
    day = today.isoformat() if isinstance(today, date) else (today or date.today().isoformat())
    user = (f"TODAY: {day}\nCLAIM: {claim.text}\nGOAL: {claim.goal}\n\n"
            f"{'PAGE' if page is not None else 'SOURCES'}:\n{render_sources(sources, page=page is not None)}")
    response = await llm.complete("checker", VERIFY_PROMPT, [{"role": "user", "content": user}],
                                  json_schema=_VERIFY_SCHEMA)
    try:
        check = parse_check(response.text, sources)
    except ValueError:
        check = ClaimCheck(verdict="INSUFFICIENT", why="verifier reply was not valid JSON", note="malformed")
    check.cost_usd = response.cost_usd
    check._usage = response  # type: ignore[attr-defined]
    return check


# Orchestration

class _Recorder:
    """Forward events to the memo's EventLog and keep them for the report trace and cost."""

    def __init__(self, log: EventLog | None) -> None:
        self.log = log
        self.events: list[dict[str, Any]] = []

    def emit(self, type: str, **data: Any) -> Any:
        self.events.append({"type": type, **({"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}),
                            "data": data})
        if self.log is not None:
            return self.log.emit(type, **data)  # type: ignore[arg-type]
        return None

    def cost(self) -> float:
        return sum(float(e["data"].get("cost_usd") or 0) for e in self.events if e["type"] in {"search", "fetch", "llm"})


class _Meter:
    """Spend so far (from recorded events) plus in-flight reservations, against the memo cap."""

    def __init__(self, cap: float, recorder: _Recorder) -> None:
        self.cap, self.recorder, self.reserved, self.hit = cap, recorder, 0.0, False

    def remaining(self) -> float:
        return self.cap - self.recorder.cost() - self.reserved

    def reserve(self, estimate: float) -> float:
        if estimate > self.remaining() + 1e-12:
            self.hit = True
            raise CostCapReached(f"Stopped at the ${self.cap:.2f} cost cap before this claim finished.")
        self.reserved += estimate
        return estimate

    def release(self, estimate: float) -> None:
        self.reserved = max(0.0, self.reserved - estimate)


def _llm_event(rec: _Recorder, check_or_response: Any, claim_id: str | None, step: str) -> None:
    usage = getattr(check_or_response, "_usage", None) or check_or_response
    rec.emit("llm", role="checker", model=getattr(usage, "model", "") or config.CHECKER_MODEL,
             tokens_in=getattr(usage, "tokens_in", 0), tokens_out=getattr(usage, "tokens_out", 0),
             cost_usd=getattr(usage, "cost_usd", 0.0), latency_ms=getattr(usage, "latency_ms", 0.0),
             cached=getattr(usage, "cached", False), claim_id=claim_id, step=step)


def authoritative_domains(results: list[Result]) -> list[str]:
    """SEC, major business press and any investor relations hosts seen in the first pass."""
    hosts = []
    for result in results:
        host = (urlsplit(result.url).hostname or "").lower()
        if host.startswith(("investor.", "investors.", "ir.")):
            hosts.append(host)
    return list(dict.fromkeys(hosts + AUTHORITATIVE_DOMAINS))[:20]


@dataclass
class _Memo:
    run_id: str
    rec: _Recorder
    meter: _Meter
    today: date
    deadline: float
    engine: str
    session: str | None = None
    timed_out: bool = False


async def _verify_step(memo: _Memo, claim: Claim, res: ClaimResult, step: str, results: list[Result],
                       page: Source | None = None) -> ClaimCheck:
    held = memo.meter.reserve(LLM_STEP_ESTIMATE_USD)
    try:
        check = await verify_claim(claim, results, memo.today, page=page)
    finally:
        memo.meter.release(held)
    if check._usage is not None:
        _llm_event(memo.rec, check, claim.id, step)
    res.cost_usd += check.cost_usd
    memo.rec.emit("check", claim_id=claim.id, step=step, verdict=check.verdict, confidence=check.confidence,
                  why=check.why, evidence_rank=check.evidence_rank, source_url=check.source_url, note=check.note)
    res.trace.append({"step": step, "verdict": check.verdict, "confidence": check.confidence, "why": check.why,
                      "source_url": check.source_url, "n_sources": 1 if page else len(_sources(results)),
                      "note": check.note})
    return check


def _finish(res: ClaimResult, check: ClaimCheck | None) -> None:
    if check is not None and check.verdict in DECISIVE:
        res.verdict = check.verdict  # type: ignore[assignment]
        res.confidence, res.why = check.confidence, check.why
        res.evidence_snippet, res.source_url = check.evidence_snippet, check.source_url
        return
    res.verdict, res.evidence_snippet, res.source_url = "NOT_FOUND", "", None
    res.confidence = check.confidence if check else 0.0
    tries = 1 + res.rescues_used
    res.why = (f"No source confirmed or refuted this after {tries} {'try' if tries == 1 else 'tries'}."
               + (f" Last check: {check.why}" if check and check.why else ""))[:300]


async def _check_claim(memo: _Memo, claim: Claim, res: ClaimResult, first: list[Result]) -> None:
    check: ClaimCheck | None = None
    try:
        check = await _verify_step(memo, claim, res, "first_pass", first)
        last = first
        if check.verdict not in DECISIVE and claim.kind in ("number", "event"):
            price = config.PRICE_USD.get(memo.engine, config.DEFAULT_PRICE_USD)
            held = memo.meter.reserve(price)
            try:
                batch = await telem_batch.search_batch(
                    [claim.query], [memo.engine], num_results=NUM_RESULTS, goal=claim.goal, session=memo.session,
                    context=CONTEXT, metadata={"backstop_version": BACKSTOP_VERSION, "run_id": memo.run_id,
                                               "claim_id": claim.id, "rescue_step": "authoritative_domains"},
                    include_domains=authoritative_domains(first))
            finally:
                memo.meter.release(held)
            run = batch.runs[0].get(memo.engine) if batch.runs else None
            results = list(run.results) if run else []
            memo.rec.emit("search", query=claim.query, goal=claim.goal, engines=[memo.engine],
                          n_results=len(results), latency_ms=batch.latency_ms, cost_usd=run.cost_usd if run else 0,
                          cached=batch.cached, claim_id=claim.id, step="authoritative_domains")
            res.rescues_used += 1
            res.engines_used = list(dict.fromkeys(res.engines_used + [memo.engine]))
            if results:
                check = await _verify_step(memo, claim, res, "authoritative_domains", results)
                last = results
        if check.verdict not in DECISIVE:
            left = memo.deadline - perf_counter()
            budget = min(config.SEARCH_MAX_COST_USD, memo.meter.remaining() - LLM_STEP_ESTIMATE_USD)
            if left < 3:
                memo.timed_out = True
                raise TimeLimitReached("Stopped at the time limit before this claim finished.")
            if budget < 0.006:
                memo.meter.hit = True
                raise CostCapReached(f"Stopped at the ${memo.meter.cap:.2f} cost cap before this claim finished.")
            held = memo.meter.reserve(budget)
            try:
                ctx = RunContext(run_id=memo.run_id, question_id=claim.id, setup="second_opinion",
                                 session=memo.session, log=memo.rec, today=memo.today,
                                 metadata={"memo_claim": claim.id})
                limits = second_opinion.SearchLimits.from_config(
                    max_cost_usd=budget, timeout_s=max(2.0, min(config.SEARCH_TIMEOUT_S, left - 1)))
                outcome = await second_opinion.search(claim.query, claim.goal, ctx, limits=limits)
            finally:
                memo.meter.release(held)
            res.rescues_used += 1
            res.cost_usd += outcome.cost_usd
            res.engines_used = list(dict.fromkeys(res.engines_used + list(outcome.engines_called)))
            res.trace.append({"step": "backstop_cascade", "status": outcome.status, "calls": outcome.calls,
                              "fetches": outcome.fetches, "engines": outcome.engines_called,
                              "cost_usd": outcome.cost_usd})
            if outcome.results:
                check = await _verify_step(memo, claim, res, "backstop_cascade", list(outcome.results))
                last = list(outcome.results)
        if check.verdict not in DECISIVE:
            url = check.source_url or next((r.url for r in last), None)
            if url:
                held = memo.meter.reserve(config.TAVILY_EXTRACT_PRICE_USD + LLM_STEP_ESTIMATE_USD)
                try:
                    page = await tavily_client.tavily_extract(url, goal=claim.goal)
                finally:
                    memo.meter.release(held)
                memo.rec.emit("fetch", url=url, ok=page.ok, via="tavily", latency_ms=page.latency_ms,
                              cost_usd=page.cost_usd, cached=page.cached, claim_id=claim.id)
                res.rescues_used += 1
                res.cost_usd += page.cost_usd
                if page.ok and page.text.strip():
                    title = next((r.title for r in last if r.url == url), "")
                    check = await _verify_step(memo, claim, res, "page_read", [],
                                               page=Source(title=title, url=url, text=page.text))
    except (CostCapReached, TimeLimitReached) as exc:
        res.error = str(exc)
        res.trace.append({"step": "stopped", "error": str(exc)})
        _finish(res, None)
        res.why = str(exc)
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # fail open: only this claim is affected
        res.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        res.trace.append({"step": "error", "error": res.error})
        _finish(res, check if check and check.verdict in DECISIVE else None)
        if res.verdict == "NOT_FOUND":
            res.why = "This claim could not be checked because of an error; see its trace."
        return
    _finish(res, check)


def _report(memo: _Memo, text: str, started: float, claims: list[ClaimResult], notes: list[str],
            interaction_id: str | None) -> MemoReport:
    events = memo.rec.events
    totals = MemoTotals(verified=sum(c.verdict == "VERIFIED" for c in claims),
                        contradicted=sum(c.verdict == "CONTRADICTED" for c in claims),
                        not_found=sum(c.verdict == "NOT_FOUND" for c in claims))
    engine_calls = sum(len(e["data"].get("engines") or []) for e in events if e["type"] == "search")
    fetches = sum(e["type"] == "fetch" for e in events)
    cost_llm = sum(float(e["data"].get("cost_usd") or 0) for e in events if e["type"] == "llm")
    cost = memo.rec.cost()
    latency = (perf_counter() - started) * 1000
    memo.rec.emit("run_end", engine_calls=engine_calls, fetches=fetches, searches=len(claims),
                  cost_usd_search=cost - cost_llm, cost_usd_llm=cost_llm, cost_usd_total=cost, latency_ms=latency,
                  verified=totals.verified, contradicted=totals.contradicted, not_found=totals.not_found)
    return MemoReport(
        run_id=memo.run_id, text=text, checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        claims=claims, totals=totals, engine_calls=engine_calls, fetches=fetches, cost_usd=round(cost, 6),
        latency_ms=round(latency, 1), telem_session_id=memo.session, telem_interaction_id=interaction_id,
        limits={"max_claims": MAX_CLAIMS, "max_memo_chars": MAX_MEMO_CHARS, "max_cost_usd": memo.meter.cap,
                "timeout_s": TIMEOUT_S, "concurrency": CONCURRENCY},
        notes=notes, trace=list(events))


async def check_memo(text: str, *, today: date | None = None, log_dir: str | Path | None = None,
                     max_cost_usd: float = MAX_COST_USD, timeout_s: float = TIMEOUT_S) -> MemoReport:
    """Extract, batch-search, verify and rescue every claim in a memo; return the full report."""
    text = validate_memo(text)
    started = perf_counter()
    run_id = f"memo-{uuid4().hex[:12]}"
    try:
        log: EventLog | None = EventLog(run_id, run_id, "second_opinion", log_dir=log_dir)
    except OSError:
        log = None
    rec = _Recorder(log)
    memo = _Memo(run_id=run_id, rec=rec, meter=_Meter(max_cost_usd, rec), today=today or date.today(),
                 deadline=started + timeout_s, engine=config.FIRST_ENGINE)
    notes: list[str] = []
    interaction_id: str | None = None
    rec.emit("run_start", question=text[:300], kind="memo_check", memo_chars=len(text))

    def left() -> float:
        return max(0.0, memo.deadline - perf_counter())

    try:
        held = memo.meter.reserve(LLM_STEP_ESTIMATE_USD)
        try:
            claims, usage = await asyncio.wait_for(_extract(text), timeout=left())
        finally:
            memo.meter.release(held)
        _llm_event(rec, usage, None, "extract_claims")
    except asyncio.TimeoutError:
        notes.append(f"Stopped at the {timeout_s:g} second time limit while extracting claims.")
        return _report(memo, text, started, [], notes, None)
    except CostCapReached as exc:
        notes.append(str(exc))
        return _report(memo, text, started, [], notes, None)
    except Exception as exc:
        notes.append(f"Claim extraction failed ({type(exc).__name__}); nothing was checked.")
        return _report(memo, text, started, [], notes, None)
    if not claims:
        notes.append("No checkable factual claims were found in this memo.")
        return _report(memo, text, started, [], notes, None)

    results = [ClaimResult(**claim.model_dump()) for claim in claims]
    first: list[list[Result]] = [[] for _ in claims]
    estimate = len(claims) * config.PRICE_USD.get(memo.engine, config.DEFAULT_PRICE_USD)
    try:
        held = memo.meter.reserve(estimate)
        try:
            batch = await asyncio.wait_for(telem_batch.search_batch(
                [c.query for c in claims], [memo.engine], num_results=NUM_RESULTS, goal=BATCH_GOAL,
                session=None, context=CONTEXT,
                metadata={"backstop_version": BACKSTOP_VERSION, "run_id": run_id,
                          "claim_ids": ",".join(c.id for c in claims), "rescue_step": "memo_first_pass"}),
                timeout=left())
        finally:
            memo.meter.release(held)
        memo.session, interaction_id = batch.session_id, batch.interaction_id
        for index, (claim, runs) in enumerate(zip(claims, batch.runs)):
            run = runs.get(memo.engine)
            first[index] = list(run.results) if run and run.status == "ok" else []
            results[index].engines_used = [memo.engine]
            rec.emit("search", query=claim.query, goal=claim.goal, engines=[memo.engine], n_results=len(first[index]),
                     latency_ms=run.latency_ms if run else batch.latency_ms, cost_usd=run.cost_usd if run else 0,
                     cached=batch.cached, claim_id=claim.id, batch_index=index, session_id=batch.session_id,
                     interaction_id=batch.interaction_id)
    except asyncio.TimeoutError:
        notes.append(f"Stopped at the {timeout_s:g} second time limit during the batched first pass.")
        for res in results:
            res.why = notes[-1]
        return _report(memo, text, started, results, notes, None)
    except CostCapReached as exc:
        notes.append(str(exc))
        for res in results:
            res.why = str(exc)
        return _report(memo, text, started, results, notes, None)
    except Exception as exc:
        notes.append(f"The batched first pass failed ({type(exc).__name__}); claims went straight to the cascade.")

    gate = asyncio.Semaphore(CONCURRENCY)

    async def run(index: int) -> None:
        async with gate:
            await _check_claim(memo, claims[index], results[index], first[index])

    tasks = [asyncio.create_task(run(i)) for i in range(len(claims))]
    done, pending = await asyncio.wait(tasks, timeout=left())
    if pending:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        message = f"Stopped at the {timeout_s:g} second time limit before this claim finished."
        notes.append(f"Stopped at the {timeout_s:g} second time limit; {len(pending)} claim(s) did not finish.")
        for task, res in zip(tasks, results):
            if task in pending:
                res.verdict, res.evidence_snippet, res.source_url = "NOT_FOUND", "", None
                res.why, res.error = message, message
                res.trace.append({"step": "stopped", "error": message})
    if memo.timed_out and not pending:
        notes.append(f"Stopped at the {timeout_s:g} second time limit; some claims were not fully checked.")
    if memo.meter.hit:
        notes.append(f"Stopped at the ${max_cost_usd:.2f} cost cap; some claims were not fully checked.")
    for res in results:
        rec.emit("answer", claim_id=res.id, final=f"{res.verdict}: {res.text}",
                 status="ANSWERED" if res.verdict in DECISIVE else "NOT_FOUND",
                 citations=[res.source_url] if res.source_url else [])
    return _report(memo, text, started, results, notes, interaction_id)


def _main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Check every factual claim in a memo through Backstop on Telem")
    parser.add_argument("path", nargs="?", help="memo text file (default: stdin)")
    args = parser.parse_args()
    text = Path(args.path).read_text() if args.path else sys.stdin.read()
    print(asyncio.run(check_memo(text)).model_dump_json(indent=2))


if __name__ == "__main__":
    _main()
