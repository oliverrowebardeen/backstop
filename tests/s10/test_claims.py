"""Memo Check unit tests: fake Telem, fake LLM and fake Extract only (all API keys blank)."""
from __future__ import annotations

import json
import os

import pytest

from so import claims, config, telem_batch
from so.contracts import PageText, SearchOutcome
from so.eventlog import read_log
from tests.s10.fakes import CLAIMS, MEMO, Fakes, result, verdict

C1, C2, C3, C4 = (c["text"] for c in CLAIMS)
WIKI = "https://en.wikipedia.org/wiki/Stripe,_Inc."


def test_api_keys_are_blank() -> None:
    for name in ("TELEM_API_KEY", "TAVILY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert os.environ.get(name, "") == ""


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Fakes:
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    return Fakes().install(monkeypatch)


# Part 1: extraction

def test_parse_claims_spans_and_fields() -> None:
    parsed = claims.parse_claims(json.dumps({"claims": CLAIMS}), MEMO)
    assert [c.id for c in parsed] == ["c1", "c2", "c3", "c4"]
    assert parsed[0].kind == "date" and parsed[0].entities == ["Stripe"]
    for claim in parsed:
        start, end = claim.span
        assert MEMO[start:end].lower() == claim.quote.lower()


def test_parse_claims_caps_at_twelve_and_tolerates_bad_rows() -> None:
    rows = [{"text": f"Fact {i}.", "quote": "", "kind": "weird", "entities": [], "query": "", "goal": ""}
            for i in range(20)]
    rows.insert(0, "not a dict")
    rows.insert(1, {"text": "   "})
    parsed = claims.parse_claims("```json\n" + json.dumps({"claims": rows}) + "\n```", "memo")
    assert len(parsed) == claims.MAX_CLAIMS == 12
    assert parsed[0].kind == "other" and parsed[0].span is None
    assert parsed[0].query == "Fact 0." and parsed[0].goal


def test_locate_is_whitespace_and_case_insensitive_and_avoids_taken_spans() -> None:
    text = "Revenue was $5B.  Revenue   WAS $5B again."
    first = claims.locate(text, "revenue was $5B")
    assert first == (0, 15)
    assert claims.locate(text, "revenue was $5B", [first])[0] > 15
    assert claims.locate(text, "not there") is None


async def test_extract_claims_uses_one_call_with_a_scoped_token_override(fakes: Fakes) -> None:
    out = await claims.extract_claims(MEMO)
    assert len(out) == 4 and len(fakes.llm_calls) == 1
    assert fakes.extract_max_tokens == claims.EXTRACT_MAX_TOKENS
    # Outside the extraction call every checker keeps its configured budget.
    assert config.LLM_MAX_TOKENS["checker"] == config.CHECKER_MAX_TOKENS


def test_validate_memo_limits() -> None:
    with pytest.raises(claims.MemoError):
        claims.validate_memo("   ")
    with pytest.raises(claims.MemoError):
        claims.validate_memo("x" * (claims.MAX_MEMO_CHARS + 1))


# Part 2: batching

def test_split_by_batch_maps_runs_back_by_batch_index() -> None:
    raw = {"session_id": "s", "by_provider": [
        {"provider": "tavily", "status": "succeeded", "batch_index": 2, "query": "q2",
         "results": [{"url": "https://c", "title": "C"}]},
        {"provider": "tavily", "status": "succeeded", "batch_index": 0, "query": "q0",
         "results": [{"url": "https://a", "title": "A"}]},
        {"provider": "tavily", "status": "succeeded", "batch_index": None, "query": "q1",
         "results": [{"url": "https://b", "title": "B"}]},
    ]}
    groups = telem_batch.split_by_batch(raw, ["q0", "q1", "q2"], ["tavily"], 5.0)
    assert [g["tavily"].results[0].url for g in groups] == ["https://a", "https://b", "https://c"]
    assert all(g["tavily"].cost_usd == config.PRICE_USD["tavily"] for g in groups)


def test_split_by_batch_marks_missing_runs_as_errors() -> None:
    groups = telem_batch.split_by_batch({"by_provider": []}, ["q0", "q1"], ["tavily"], 5.0)
    assert [g["tavily"].status for g in groups] == ["error", "error"]


async def test_search_batch_sends_one_interaction_and_caches(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    sent: list[dict] = []

    class Response:
        def __init__(self, raw: dict) -> None:
            self.raw = raw

        def model_dump(self, mode: str = "json") -> dict:
            return self.raw

    class FakeTelem:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def search(self, **request):
            sent.append(request)
            runs = [{"provider": "tavily", "status": "succeeded", "batch_index": i, "query": q,
                     "results": [{"url": f"https://r/{i}", "title": q}]} for i, q in enumerate(request["query"])]
            return Response({"by_provider": runs, "session_id": "sess-9", "interaction_id": "int-9"})

    monkeypatch.setenv("TELEM_API_KEY", "fake-key-for-fake-client")
    monkeypatch.setattr(telem_batch, "AsyncTelem", FakeTelem)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "SO_CACHE", "readwrite")
    out = await telem_batch.search_batch(["a", "b"], ["tavily"], num_results=5, goal="g", context="backstop memo check",
                                         metadata={"backstop_version": "x"}, include_domains=["sec.gov"])
    assert len(sent) == 1 and sent[0]["query"] == ["a", "b"] and sent[0]["providers_include"] == ["tavily"]
    assert sent[0]["include_domains"] == ["sec.gov"] and sent[0]["context"] == "backstop memo check"
    assert sent[0]["metadata"] == {"backstop_version": "x"} and sent[0]["num_results"] == 5
    assert out.session_id == "sess-9" and out.interaction_id == "int-9"
    assert [r["tavily"].results[0].url for r in out.runs] == ["https://r/0", "https://r/1"]
    again = await telem_batch.search_batch(["a", "b"], ["tavily"], num_results=5, goal="g",
                                           include_domains=["sec.gov"], session="sess-9")
    assert len(sent) == 1 and again.cached and again.runs[1]["tavily"].results[0].url == "https://r/1"


async def test_memo_first_pass_is_one_batched_call(fakes: Fakes, tmp_path) -> None:
    fakes.first = {c["query"]: [result(WIKI, "Stripe was founded in 2010.")] for c in CLAIMS}
    fakes.verify = {c["text"]: [verdict("VERIFIED", 1, "founded in 2010")] for c in CLAIMS}
    report = await claims.check_memo(MEMO, log_dir=tmp_path)
    first = fakes.batch_calls[0]
    assert len(fakes.batch_calls) == 1
    assert first["queries"] == [c["query"] for c in CLAIMS] and first["engines"] == [config.FIRST_ENGINE]
    assert first["num_results"] == 5 and first["context"] == "backstop memo check" and first["session"] is None
    assert first["metadata"]["claim_ids"] == "c1,c2,c3,c4" and "include_domains" not in first
    assert report.telem_session_id == "sess-1" and report.telem_interaction_id == "int-1"
    assert report.engine_calls == 4


# Part 3: verdict paths

async def test_verified_on_first_pass(fakes: Fakes, tmp_path) -> None:
    fakes.claims = CLAIMS[:1]
    fakes.first = {CLAIMS[0]["query"]: [result(WIKI, "Stripe, Inc. was founded in 2010 by the Collisons.")]}
    fakes.verify = {C1: [verdict("VERIFIED", 1, "was founded in 2010")]}
    report = await claims.check_memo(MEMO, log_dir=tmp_path)
    c = report.claims[0]
    assert c.verdict == "VERIFIED" and c.source_url == WIKI and c.evidence_snippet == "was founded in 2010"
    assert c.rescues_used == 0 and c.engines_used == [config.FIRST_ENGINE]
    assert fakes.cascade_calls == [] and report.totals.verified == 1


async def test_rescue_through_cascade_to_verified(fakes: Fakes, tmp_path) -> None:
    fakes.claims = CLAIMS[1:2]
    fakes.first = {CLAIMS[1]["query"]: [result(WIKI, "Stripe is a payments company.")]}
    url = "https://stripe.com/about"
    fakes.cascade = lambda q, g: SearchOutcome(status="HIT", results=[result(url, "Stripe HQ: South San Francisco",
                                                                             engine="exa")],
                                               engines_called=["tavily", "exa"], calls=2, cost_usd=0.01)
    fakes.verify = {C2: [verdict("INSUFFICIENT", None), verdict("VERIFIED", 1, "South San Francisco")]}
    report = await claims.check_memo(MEMO, log_dir=tmp_path)
    c = report.claims[0]
    assert c.verdict == "VERIFIED" and c.source_url == url and c.rescues_used == 1
    assert fakes.cascade_calls == [CLAIMS[1]["query"]] and "exa" in c.engines_used
    assert [t["step"] for t in c.trace] == ["first_pass", "backstop_cascade", "backstop_cascade"]


async def test_contradicted_needs_url_and_quote(fakes: Fakes, tmp_path) -> None:
    fakes.claims = CLAIMS[2:3]
    url = "https://stripe.com/annual-updates/2023"
    fakes.first = {CLAIMS[2]["query"]: [result(url, "Stripe's total payment volume was $1 trillion in 2023.")]}
    fakes.verify = {C3: [verdict("CONTRADICTED", 1, "total payment volume was $1 trillion in 2023",
                                 why='memo says "9 trillion", source says "$1 trillion"')]}
    report = await claims.check_memo(MEMO, log_dir=tmp_path)
    c = report.claims[0]
    assert c.verdict == "CONTRADICTED" and c.source_url == url
    assert "1 trillion" in c.evidence_snippet and "9 trillion" in c.why
    assert report.totals.contradicted == 1


async def test_contradicted_without_url_is_downgraded_to_not_found(fakes: Fakes, tmp_path) -> None:
    fakes.claims = CLAIMS[2:3]
    fakes.first = {CLAIMS[2]["query"]: [result("https://x.com/a", "Stripe volume figures")]}
    fakes.verify = {C3: [verdict("CONTRADICTED", None, "made up quote")]}
    report = await claims.check_memo(MEMO, log_dir=tmp_path)
    c = report.claims[0]
    assert c.verdict == "NOT_FOUND" and c.source_url is None and c.evidence_snippet == ""
    assert "not accepted" in (c.trace[0]["note"] or "")


def test_contradicted_without_quote_is_not_accepted() -> None:
    source = claims.Source(title="T", url="https://a", text="")
    check = claims.parse_check(json.dumps(verdict("CONTRADICTED", 1, "")), [source])
    assert check.verdict == "INSUFFICIENT" and check.source_url is None


def test_low_confidence_contradiction_is_not_accepted() -> None:
    source = claims.Source(title="T", url="https://a", text="Code names: Celsius, Kelvin.")
    check = claims.parse_check(json.dumps(verdict("CONTRADICTED", 1, "Celsius", confidence=0.3)), [source])
    assert check.verdict == "INSUFFICIENT" and check.source_url is None and "below" in check.note


def test_non_verbatim_quote_falls_back_to_source_text() -> None:
    source = claims.Source(title="T", url="https://a", text="Stripe was founded in 2010 in Palo Alto.")
    check = claims.parse_check(json.dumps(verdict("VERIFIED", 1, "Stripe began life in 2010")), [source])
    assert check.verdict == "VERIFIED" and check.evidence_snippet.startswith("Stripe was founded")
    assert check.note and "not verbatim" in check.note


def test_out_of_range_rank_never_yields_a_url() -> None:
    source = claims.Source(title="T", url="https://a", text="x")
    check = claims.parse_check(json.dumps(verdict("VERIFIED", 7, "x")), [source])
    assert check.verdict == "INSUFFICIENT" and check.source_url is None


async def test_number_claim_gets_authoritative_domain_retry(fakes: Fakes, tmp_path) -> None:
    fakes.claims = CLAIMS[2:3]
    fakes.first = {CLAIMS[2]["query"]: [result("https://investor.stripe.com/x", "Stripe news")]}
    sec = "https://www.sec.gov/doc"
    fakes.domain_results = [result(sec, "Total payment volume of $1.0 trillion in 2023")]
    fakes.verify = {C3: [verdict("INSUFFICIENT", None),
                         verdict("CONTRADICTED", 1, "Total payment volume of $1.0 trillion")]}
    report = await claims.check_memo(MEMO, log_dir=tmp_path)
    domains = fakes.batch_calls[1]["include_domains"]
    assert "sec.gov" in domains and "investor.stripe.com" in domains
    assert report.claims[0].verdict == "CONTRADICTED" and report.claims[0].source_url == sec
    assert fakes.cascade_calls == []


async def test_page_read_rescue(fakes: Fakes, tmp_path) -> None:
    fakes.claims = CLAIMS[:1]
    fakes.first = {CLAIMS[0]["query"]: [result(WIKI, "Stripe is a payments company.")]}
    fakes.page = PageText(url=WIKI, ok=True, text="Stripe was founded in 2010 by Patrick and John Collison.",
                          cost_usd=0.0016)
    fakes.verify = {C1: [verdict("INSUFFICIENT", 1), verdict("VERIFIED", 1, "Stripe was founded in 2010")]}
    report = await claims.check_memo(MEMO, log_dir=tmp_path)
    c = report.claims[0]
    assert fakes.extract_calls == [WIKI] and c.verdict == "VERIFIED" and c.rescues_used == 2
    assert report.fetches == 1


async def test_not_found_after_the_cascade(fakes: Fakes, tmp_path) -> None:
    fakes.claims = CLAIMS[3:4]
    report = await claims.check_memo(MEMO, log_dir=tmp_path)
    c = report.claims[0]
    assert c.verdict == "NOT_FOUND" and c.source_url is None
    assert fakes.cascade_calls == [CLAIMS[3]["query"]] and report.totals.not_found == 1
    assert "No source confirmed" in c.why


# Part 4: orchestration, fail open and limits

async def test_one_claims_error_marks_only_that_claim(fakes: Fakes, tmp_path) -> None:
    fakes.claims = CLAIMS[:2]
    fakes.first = {c["query"]: [result(WIKI, "Stripe facts")] for c in CLAIMS}
    fakes.verify = {C2: [verdict("VERIFIED", 1, "Stripe facts")]}
    fakes.verify_error_for = C1
    report = await claims.check_memo(MEMO, log_dir=tmp_path)
    bad, good = report.claims
    assert bad.verdict == "NOT_FOUND" and "verifier exploded" in bad.error
    assert any(t["step"] == "error" for t in bad.trace)
    assert good.verdict == "VERIFIED" and good.error is None


async def test_batch_failure_fails_open_to_the_cascade(fakes: Fakes, tmp_path) -> None:
    fakes.claims = CLAIMS[:1]
    fakes.batch_error = RuntimeError("router down")
    report = await claims.check_memo(MEMO, log_dir=tmp_path)
    assert fakes.cascade_calls == [CLAIMS[0]["query"]]
    assert any("first pass failed" in n for n in report.notes)


async def test_cost_cap_stops_work_with_a_note(fakes: Fakes, tmp_path) -> None:
    report = await claims.check_memo(MEMO, log_dir=tmp_path, max_cost_usd=0.012)
    assert report.cost_usd <= 0.012 + 1e-9
    assert any("cost cap" in n for n in report.notes)
    assert all(c.verdict == "NOT_FOUND" for c in report.claims)
    assert any("cost cap" in (c.why or "") for c in report.claims)
    assert fakes.cascade_calls == []


async def test_timeout_stops_work_with_a_note(fakes: Fakes, tmp_path) -> None:
    fakes.verify_delay = 5.0
    fakes.first = {c["query"]: [result(WIKI, "Stripe facts")] for c in CLAIMS}
    report = await claims.check_memo(MEMO, log_dir=tmp_path, timeout_s=0.3)
    assert any("time limit" in n for n in report.notes)
    assert all(c.verdict == "NOT_FOUND" and "time limit" in c.why for c in report.claims)
    assert report.latency_ms < 3000


async def test_report_shape_log_and_costs(fakes: Fakes, tmp_path) -> None:
    fakes.first = {c["query"]: [result(WIKI, "Stripe facts")] for c in CLAIMS}
    fakes.verify = {C1: [verdict("VERIFIED", 1, "Stripe facts")]}
    report = await claims.check_memo(MEMO, log_dir=tmp_path)
    assert report.run_id.startswith("memo-")
    assert report.totals.verified + report.totals.contradicted + report.totals.not_found == 4
    # Extraction, every verify call, every search and every cascade cost are counted.
    assert report.cost_usd >= 0.001 + 4 * 0.005 + 4 * 0.0002
    events = read_log(tmp_path / f"{report.run_id}.jsonl")
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end" and "llm" in kinds and "check" in kinds
    assert events[-1]["data"]["cost_usd_total"] == pytest.approx(report.cost_usd, abs=1e-6)
    json.loads(report.model_dump_json())


async def test_no_claims_found(fakes: Fakes, tmp_path) -> None:
    fakes.claims = []
    report = await claims.check_memo("We are excited about the future.", log_dir=tmp_path)
    assert report.claims == [] and "No checkable" in report.notes[0] and fakes.batch_calls == []
