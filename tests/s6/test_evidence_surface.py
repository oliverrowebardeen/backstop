"""Follow-up s6-evidence: the HIT evidence object and ranking reach the client and the hosted API."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backstop import BackstopEvidence, BackstopRankEntry, BackstopTelem
from server import api_v1
from server.usage import RateLimiter, UsageStore
from so.client import _response
from so.contracts import Result, SearchOutcome
from so.contracts import CheckerVerdict
from tests.s6.fakes import verdict
from tests.s6.test_client import FakeAsyncTelem, install_checker

WHY = 'Result 2 says the page is the "Evidence page" for this goal.'


def hit_steps():
    return [verdict("DEAD", rewrite="q2"), CheckerVerdict(verdict="HIT", confidence=0.9, why=WHY, evidence_rank=2)]


async def test_client_hit_carries_verified_evidence_and_ranking(monkeypatch):
    install_checker(monkeypatch, hit_steps())
    resp = await BackstopTelem(telem=FakeAsyncTelem()).search("q", goal="g")
    assert resp.status == "HIT"
    assert isinstance(resp.evidence, BackstopEvidence)
    assert resp.evidence.verified is True
    assert resp.evidence.url == "https://evidence.example" == resp.results[0].url
    assert "Evidence page" in resp.evidence.snippet
    assert resp.evidence.agreeing_engines == ["exa"] and resp.evidence.engines_agreeing == 1
    assert resp.evidence.source == "search"
    done = next(e["data"] for e in reversed(resp.trace) if e["type"] == "search_done")
    assert resp.evidence.model_dump(exclude_none=True) == {
        key: value for key, value in done["evidence"].items() if value is not None}
    assert [entry.rank_reason for entry in resp.ranking] == [r.rank_reason for r in resp.results]
    assert resp.ranking[0] == BackstopRankEntry(url="https://evidence.example", rank=1,
                                                rank_reason="evidence", engines=["exa"])
    assert resp.ranking[1].rank_reason == "agreement: 2 engines"


async def test_not_found_and_unchecked_have_no_evidence(monkeypatch):
    install_checker(monkeypatch, [verdict("DEAD")] * 3)
    missed = await BackstopTelem(telem=FakeAsyncTelem()).search("q", max_rescues=0)
    assert missed.status == "NOT_FOUND" and missed.evidence is None
    assert missed.ranking and missed.ranking[0].rank_reason.startswith("engine rank")
    install_checker(monkeypatch, [RuntimeError("model down")])
    unchecked = await BackstopTelem(telem=FakeAsyncTelem()).search("q")
    assert unchecked.status == "UNCHECKED" and unchecked.evidence is None


def test_unknown_evidence_keys_are_kept_and_empty_results_give_no_ranking():
    trace = [{"type": "search_done", "data": {"status": "HIT", "ranking": [],
              "evidence": {"verified": True, "url": "https://a", "snippet": "s", "engines_agreeing": 1,
                           "agreeing_engines": ["exa"], "source": "tavily_extract", "chunks": 3,
                           "future_field": "kept"}}}]
    resp = _response(SearchOutcome(status="HIT", results=[], trace=trace), [], None)
    assert resp.evidence.chunks == 3 and resp.evidence.model_dump()["future_field"] == "kept"
    assert resp.ranking is None
    plain = _response(SearchOutcome(status="UNCHECKED", results=[Result(rank=1, title="t", url="https://b",
                                                                       engine="tavily")]), [], None)
    assert plain.evidence is None and plain.ranking[0].rank_reason == "engine rank 1"


def test_existing_fields_unchanged_and_new_fields_optional():
    from so.client import BackstopSearchResponse

    fields = BackstopSearchResponse.model_fields
    assert not fields["evidence"].is_required() and not fields["ranking"].is_required()
    assert BackstopSearchResponse(status="HIT").evidence is None


def test_api_returns_evidence_and_ranking(monkeypatch, tmp_path):
    install_checker(monkeypatch, hit_steps())
    monkeypatch.setenv("BACKSTOP_API_KEYS", "bk_test")
    app = FastAPI()
    app.include_router(api_v1.router)
    app.dependency_overrides[api_v1.get_client] = lambda: BackstopTelem(telem=FakeAsyncTelem())
    app.dependency_overrides[api_v1.get_usage_store] = lambda: UsageStore(tmp_path / "u.db")
    app.dependency_overrides[api_v1.get_rate_limiter] = lambda: RateLimiter()
    body = TestClient(app).post("/v1/search", headers={"Authorization": "Bearer bk_test"},
                                json={"query": "q", "goal": "g"}).json()
    assert body["status"] == "HIT"
    assert body["evidence"]["verified"] is True and body["evidence"]["url"] == "https://evidence.example"
    assert body["evidence"]["agreeing_engines"] == ["exa"]
    assert body["ranking"][0] == {"url": "https://evidence.example", "rank": 1, "rank_reason": "evidence",
                                  "engines": ["exa"]}
    assert body["results"][0]["rank_reason"] == "evidence"


@pytest.mark.parametrize("name", ["evidence", "ranking"])
def test_openapi_documents_optional_fields(name):
    app = FastAPI()
    app.include_router(api_v1.router)
    schema = app.openapi()["components"]["schemas"]["BackstopSearchResponse"]
    assert name in schema["properties"] and name not in schema.get("required", [])
