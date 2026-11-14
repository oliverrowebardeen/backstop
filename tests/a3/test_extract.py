"""Tavily Extract chunk requests and their cost, with a mocked AsyncTavilyClient."""

import pytest

from so import config, tavily_client


def fake_client(monkeypatch, response):
    class Client:
        calls: list[dict] = []

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def extract(self, **kwargs):
            self.calls.append(kwargs)
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(tavily_client, "AsyncTavilyClient", Client)
    monkeypatch.setattr(config, "SO_CACHE", "readwrite")
    monkeypatch.setattr(config, "SEARCH_BUDGET", config.Budget(1))
    monkeypatch.setenv("TAVILY_API_KEY", "test-placeholder")
    return Client


async def test_goal_requests_three_basic_chunks_with_ten_second_timeout(monkeypatch):
    raw = "First chunk [...] Revenue was $4.2 billion [...] Third chunk"
    client = fake_client(monkeypatch, {"results": [{"url": "https://a.example", "raw_content": raw}]})
    page = await tavily_client.tavily_extract("https://a.example", goal="find the revenue")
    request = client.calls[0]
    assert request["urls"] == ["https://a.example"]
    assert request["query"] == "find the revenue"
    assert request["chunks_per_source"] == 3
    assert request["extract_depth"] == "basic"
    assert request["timeout"] == 10
    assert page.ok and tavily_client.split_chunks(page.text) == ["First chunk", "Revenue was $4.2 billion", "Third chunk"]
    assert page.cost_usd == pytest.approx(config.TAVILY_EXTRACT_PRICE_USD)
    assert config.SEARCH_BUDGET.spent == pytest.approx(config.TAVILY_EXTRACT_PRICE_USD)


async def test_extract_price_is_one_fifth_of_a_credit():
    assert config.TAVILY_EXTRACT_PRICE_USD == pytest.approx(config.TAVILY_CREDIT_USD / 5)


async def test_no_goal_sends_no_chunks(monkeypatch):
    client = fake_client(monkeypatch, {"results": [{"url": "https://a.example", "raw_content": "page"}]})
    await tavily_client.tavily_extract("https://a.example")
    assert "query" not in client.calls[0] and "chunks_per_source" not in client.calls[0]


async def test_failed_extract_costs_nothing(monkeypatch):
    fake_client(monkeypatch, {"results": [], "failed_results": [{"url": "https://a.example", "error": "x"}]})
    page = await tavily_client.tavily_extract("https://a.example", goal="goal")
    assert not page.ok and page.cost_usd == 0
    assert config.SEARCH_BUDGET.spent == 0


@pytest.mark.parametrize("chunks", [0, 6])
async def test_chunks_out_of_range_rejected(chunks):
    with pytest.raises(ValueError):
        await tavily_client.tavily_extract("https://a.example", goal="goal", chunks_per_source=chunks)
