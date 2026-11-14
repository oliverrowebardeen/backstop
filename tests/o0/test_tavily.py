import pytest

from so import config, tavily_client


@pytest.mark.parametrize("success", [True, False])
async def test_extract_success_failure_and_cache(tmp_path, monkeypatch, success):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "SO_CACHE", "readwrite")
    monkeypatch.setattr(config, "SEARCH_BUDGET", config.Budget(1))
    monkeypatch.setenv("TAVILY_API_KEY", "test-placeholder")

    class Client:
        calls = []

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def extract(self, **kwargs):
            self.calls.append(kwargs)
            return {"results": [{"url": "https://example.com", "raw_content": "a" * 7000}] if success else [],
                    "failed_results": [] if success else [{"url": "https://example.com", "error": "failed"}]}

    monkeypatch.setattr(tavily_client, "AsyncTavilyClient", Client)
    first = await tavily_client.tavily_extract("https://example.com", goal="find answer")
    second = await tavily_client.tavily_extract("https://example.com", goal="find answer")
    assert first.ok == success
    assert len(first.text) == (6000 if success else 0)
    assert second.cached and second.latency_ms == first.latency_ms
    assert len(Client.calls) == 1
    assert Client.calls[0]["urls"] == ["https://example.com"]
    assert Client.calls[0]["query"] == "find answer"
    assert config.SEARCH_BUDGET.spent == (config.TAVILY_EXTRACT_PRICE_USD if success else 0)
