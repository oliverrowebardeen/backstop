import asyncio

import pytest
from telem.models import FetchResponse, SearchResponse

from so import config, telem_client


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEM_API_KEY", "test-placeholder")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "SO_CACHE", "readwrite")
    monkeypatch.setattr(config, "SEARCH_BUDGET", config.Budget(1))


@pytest.fixture
def fake_client(monkeypatch):
    class Client:
        calls = []
        options = []

        def __init__(self, **kwargs):
            self.options.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def search(self, **kwargs):
            self.calls.append(kwargs)
            await asyncio.sleep(0)
            return SearchResponse(session_id="session-1", interaction_id="i1", status="succeeded", by_provider=[
                {"provider": "tavily", "status": "succeeded", "latency_ms": 321,
                 "results": [{"url": "https://example.com", "summary": "", "excerpt": ["a" * 510], "rank": 1}]},
                {"provider": "exa", "status": "failed", "results": []},
            ])

        async def fetch(self, **kwargs):
            self.calls.append(kwargs)
            return FetchResponse(session_id="session-1", interaction_id="i2", status="succeeded",
                                 results=[{"url": kwargs["urls"][0], "status": "success", "content": "x" * 6100}])

    monkeypatch.setattr(telem_client, "AsyncTelem", Client)
    return Client


async def test_normalization_cache_session_and_cost(fake_client):
    first = await telem_client.search_engines("q", ["tavily", "exa"], goal="g", session="session-1")
    second = await telem_client.search_engines("q", ["tavily", "exa"], goal="g", session="session-2")
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["session"] == "session-1"
    assert fake_client.calls[0]["goal"] == "g"
    assert len(first["tavily"].results[0].snippet) == 500
    assert first["exa"].status == "error" and first["exa"].cost_usd == 0
    assert second["tavily"].cached and second["tavily"].session_id == "session-2"
    assert second["tavily"].latency_ms == first["tavily"].latency_ms == 321
    assert second["tavily"].cost_usd == first["tavily"].cost_usd == 0.005
    assert config.SEARCH_BUDGET.spent == 0.005


async def test_default_routes_without_pinning_and_passes_goal(fake_client):
    await telem_client.search_default("q", goal="g", session="s")
    assert fake_client.calls[0]["auto_routing"] == "accuracy"
    assert "providers_include" not in fake_client.calls[0]
    assert fake_client.calls[0]["session"] == "s"


async def test_fetch_truncates_and_preserves_session(fake_client):
    page = await telem_client.fetch_page("https://example.com", session="s", goal="first")
    cached = await telem_client.fetch_page("https://example.com", session="s", goal="second")
    assert page.ok and len(page.text) == 6000
    assert fake_client.calls[0]["session"] == "s"
    assert "goal" not in fake_client.calls[0]
    assert cached.cached and cached.cost_usd == page.cost_usd == 0.001
    assert len(fake_client.calls) == 1


async def test_budget_rejects_before_network(fake_client, monkeypatch):
    monkeypatch.setattr(config, "SEARCH_BUDGET", config.Budget(0))
    with pytest.raises(config.BudgetExceeded):
        await telem_client.search_engines("q", ["tavily"])
    assert not fake_client.calls


async def test_concurrent_identical_calls_pay_once(fake_client):
    results = await asyncio.gather(*(telem_client.search_engines("q", ["tavily"]) for _ in range(8)))
    assert len(fake_client.calls) == 1
    assert sum(result["tavily"].cached for result in results) == 7


async def test_provider_endpoint_timeout_and_content_overrides(fake_client, monkeypatch):
    monkeypatch.setattr(config, "TELEM_FETCH_PROVIDER", "custom-fetch")
    monkeypatch.setattr(config, "TELEM_BASE_URL", "https://telem.example")
    monkeypatch.setattr(config, "TELEM_TIMEOUT_S", 7)
    monkeypatch.setattr(config, "PAGE_TEXT_CHARS", 123)
    page = await telem_client.fetch_page("https://example.com", session="s")
    assert fake_client.options[0]["base_url"] == "https://telem.example"
    assert fake_client.options[0]["timeout"] == 7
    assert fake_client.calls[0]["providers"] == ["custom-fetch"]
    assert len(page.text) == 123


@pytest.mark.parametrize("engines", [["tavily", "exa"], None], ids=["pinned", "auto-routing"])
async def test_partial_engine_failures_expire_but_successes_remain_cached(fake_client, monkeypatch, engines):
    now = [100.0]
    monkeypatch.setattr(config, "TELEM_ERROR_CACHE_TTL_S", 10)
    monkeypatch.setattr(telem_client.time, "time", lambda: now[0])

    async def search():
        if engines is None:
            return await telem_client.search_default("retry a partial failure")
        return await telem_client.search_engines("retry a partial failure", engines)

    first = await search()
    assert first["exa"].status == "error"
    now[0] = 109
    assert (await search())["exa"].cached
    assert len(fake_client.calls) == 1

    async def recovered(self, **kwargs):
        self.calls.append(kwargs)
        return SearchResponse(session_id="recovered", interaction_id="i3", status="succeeded", by_provider=[
            {"provider": "tavily", "status": "succeeded", "results": []},
            {"provider": "exa", "status": "succeeded", "results": []},
        ])

    monkeypatch.setattr(fake_client, "search", recovered)
    now[0] = 110
    recovered_runs = await search()
    assert recovered_runs["exa"].status == "ok" and not recovered_runs["exa"].cached
    assert len(fake_client.calls) == 2
    assert config.SEARCH_BUDGET.spent == pytest.approx(.015)
    now[0] = 1000
    assert all(run.cached for run in (await search()).values())
    assert len(fake_client.calls) == 2


async def test_old_failed_cache_entries_are_retried_immediately(fake_client):
    import json

    await telem_client.search_engines("legacy", ["tavily", "exa"])
    path = next(config.CACHE_DIR.glob("telem-search-*.json"))
    record = json.loads(path.read_text())
    record.pop("expires_at")
    path.write_text(json.dumps(record))
    await telem_client.search_engines("legacy", ["tavily", "exa"])
    assert len(fake_client.calls) == 2


async def test_zero_failure_ttl_retries_on_next_call(fake_client, monkeypatch):
    monkeypatch.setattr(config, "TELEM_ERROR_CACHE_TTL_S", 0)
    await telem_client.search_engines("retry now", ["exa"])
    again = await telem_client.search_engines("retry now", ["exa"])
    assert len(fake_client.calls) == 2
    assert not again["exa"].cached


async def test_failed_fetch_recovers_after_cache_expiry(fake_client, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(config, "TELEM_ERROR_CACHE_TTL_S", 10)
    monkeypatch.setattr(telem_client.time, "time", lambda: now[0])
    original = fake_client.fetch

    async def unavailable(self, **kwargs):
        self.calls.append(kwargs)
        return FetchResponse(session_id="s", interaction_id="i", status="failed", results=[
            {"url": kwargs["urls"][0], "status": "failed", "error": {"message": "temporary"}},
        ])

    monkeypatch.setattr(fake_client, "fetch", unavailable)
    assert not (await telem_client.fetch_page("https://example.com/retry")).ok
    assert (await telem_client.fetch_page("https://example.com/retry")).cached
    assert len(fake_client.calls) == 1
    monkeypatch.setattr(fake_client, "fetch", original)
    now[0] = 110
    assert (await telem_client.fetch_page("https://example.com/retry")).ok
    assert len(fake_client.calls) == 2
    assert config.SEARCH_BUDGET.spent == .001
