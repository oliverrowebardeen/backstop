"""evals.retrieval: cache-key parity with so.telem_client, offline judging, checker metrics, coverage."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from evals import retrieval
from so import config, telem_client


class FakeAsyncTelem:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def search(self, **kw):
        engines = kw.get("providers_include") or ["exa", "brave"]
        return SimpleNamespace(model_dump=lambda mode="json": {
            "session_id": "s1",
            "by_provider": [{"provider": e, "status": "succeeded", "latency_ms": 5, "error": None,
                             "results": [{"url": f"https://{e}.example/1", "title": "t", "summary": "Ada",
                                          "rank": 1, "provider": e}]} for e in engines]})


@pytest.fixture()
def keys(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "SO_CACHE", "readwrite")
    monkeypatch.setattr(telem_client, "AsyncTelem", FakeAsyncTelem)
    monkeypatch.setattr(config, "api_key", lambda provider: "test")
    written = []
    real = config.write_cache
    monkeypatch.setattr(config, "write_cache", lambda key, value: (written.append(key), real(key, value)))
    return written


@pytest.mark.parametrize("engines,published_after", [(["tavily"], None), (None, None), (["exa"], "2026-08-01")])
def test_cache_key_matches_telem_client(keys, engines, published_after):
    if engines is None:
        asyncio.run(telem_client.search_default("who founded X", goal="find the founder"))
    else:
        asyncio.run(telem_client.search_engines("who founded X", engines, goal="find the founder",
                                                published_after=published_after))
    assert keys == [retrieval.search_cache_key("who founded X", engines, "find the founder", published_after)]
    got = retrieval.cached_results("Who founded X?", "who founded X", engines, "find the founder", published_after)
    assert got and all(r.snippet == "Ada" for r in got)


def test_uncached_search_is_none(keys):
    assert retrieval.cached_results("Q", "never searched", ["tavily"], None) is None


def ev(seq, type_, **data):
    return {"ts": "2026-11-14T19:00:00.000Z", "run_id": "r", "question_id": "q1", "setup": "second_opinion",
            "seq": seq, "type": type_, "data": data}


def test_checks_pair_only_with_the_search_they_checked():
    events = [ev(1, "search", query="a", engines=["tavily"]), ev(2, "llm", role="checker"),
              ev(3, "check", verdict="DEAD"), ev(4, "search", query="b", engines=["exa"]),
              ev(5, "check", verdict="SHALLOW"), ev(6, "fetch", url="u"), ev(7, "check", verdict="HIT"),
              ev(8, "search_done", status="HIT")]
    pairs = retrieval._searches_with_checks(events)
    assert [(p[0]["seq"], p[1]["verdict"]) for p in pairs] == [(1, "DEAD"), (4, "SHALLOW")]  # page re-check not paired


def S(q, seq, hit, verdict, call=0, setup="second_opinion"):
    return retrieval.JudgedSearch(q, setup, seq, ["tavily"], "x", hit, verdict, call)


def test_checker_metrics_on_exact_lists():
    truth = retrieval.RunTruth(searches=[
        S("q1", 1, False, "DEAD"), S("q1", 4, True, "HIT"),      # miss caught, rescue later hit -> DEAD wrong
        S("q2", 1, True, "HIT"),                                  # hit kept
        S("q3", 1, True, "STALE"),                                # false alarm
        S("q4", 1, False, "HIT"),                                 # missed miss
        S("q5", 1, False, "DEAD"),                                # dead and nothing later -> DEAD right
    ])
    m = retrieval.checker_metrics(truth)
    assert (m["n_true_misses"], m["n_true_hits"]) == (3, 3)
    assert m["miss_recall"] == round(2 / 3, 4) and m["false_alarm_rate"] == round(1 / 3, 4)
    # q1: rescuable but called DEAD (wrong); q4: not rescuable, called HIT (wrong); q5: DEAD and not rescuable (right)
    assert m["rescuable_vs_dead_acc"] == round(1 / 3, 4)


def test_coverage_uses_the_scored_cohort():
    truth = retrieval.RunTruth(searches=[S("q1", 1, False, None), S("q1", 2, True, None), S("q2", 1, False, None),
                                         S("q9", 1, True, None)])
    cov = retrieval.coverage_by_setup(truth, {"second_opinion": {"q1", "q2", "q3"}})["second_opinion"]
    assert (cov["covered"], cov["n_questions"], cov["coverage"]) == (1, 3, round(1 / 3, 4))
