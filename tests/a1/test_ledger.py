"""Task A: every Telem call names its Backstop step; labels are additive and fail open."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from backstop import BackstopTelem
from so import config, telem_client
from so.contracts import EngineRun, Result, RunContext
from so.ledger import BACKSTOP_VERSION, call_with_ledger, ledger
from so.second_opinion import search
from tests.a1.fakes import FakeAsyncTelem, install_checker, verdict


def test_ledger_names_step_verdict_engine_and_version():
    fields = ledger("rescue_1", "WRONG_ENTITY", "exa")
    assert fields["context"] == "backstop step=rescue_1 verdict=WRONG_ENTITY"
    assert fields["metadata"] == {"backstop_version": BACKSTOP_VERSION, "verdict": "WRONG_ENTITY",
                                  "engine": "exa", "rescue_step": "rescue_1"}
    assert ledger("initial", None, "tavily")["metadata"]["verdict"] == "none"


def test_version_matches_package():
    import backstop

    assert BACKSTOP_VERSION == backstop.__version__


async def test_rejected_ledger_is_retried_once_without_it(caplog):
    sent: list[dict[str, Any]] = []

    async def send(extra):
        sent.append(extra)
        if extra:
            from telem.errors import BadRequestError
            raise BadRequestError("metadata not allowed", status_code=400, body={})
        return "ok"

    with caplog.at_level(logging.WARNING, logger="so.ledger"):
        assert await call_with_ledger(send, ledger("initial", None, "tavily")) == "ok"
    assert sent == [ledger("initial", None, "tavily"), {}]
    assert "retrying without them" in caplog.text


@pytest.mark.parametrize("error", [RuntimeError("down"), type("E", (Exception,), {"status_code": 503})()])
async def test_other_errors_propagate_without_a_ledger_retry(error):
    sent: list[dict[str, Any]] = []

    async def send(extra):
        sent.append(extra)
        raise error

    with pytest.raises(type(error)):
        await call_with_ledger(send, ledger("initial", None, "tavily"))
    assert len(sent) == 1


async def test_no_ledger_means_no_retry():
    sent = []

    async def send(extra):
        sent.append(extra)
        raise type("E", (Exception,), {"status_code": 400})()

    with pytest.raises(Exception):
        await call_with_ledger(send, {"context": None, "metadata": None})
    assert sent == [{}]


@pytest.mark.parametrize("status", [400, 422])
async def test_telem_client_fails_open_when_router_rejects_metadata(monkeypatch, status, caplog):
    fake = FakeAsyncTelem(reject={"metadata"}, reject_status=status)
    monkeypatch.setattr(telem_client, "AsyncTelem", lambda **kwargs: fake)
    with caplog.at_level(logging.WARNING):
        runs = await telem_client.search_engines("q", ["tavily"], goal="g", **ledger("initial", None, "tavily"))
    assert runs["tavily"].status == "ok"
    assert [("metadata" in call, "context" in call) for call in fake.calls] == [(True, True), (False, False)]
    assert "HTTP %d" % status in caplog.text


async def test_telem_client_sends_goal_context_metadata_session(monkeypatch):
    fake = FakeAsyncTelem()
    monkeypatch.setattr(telem_client, "AsyncTelem", lambda **kwargs: fake)
    await telem_client.search_engines("q", ["exa"], goal="the goal", session="s-1",
                                      **ledger("rescue_2", "STALE", "exa"))
    call = fake.calls[0]
    assert call["goal"] == "the goal" and call["session"] == "s-1"
    assert call["context"] == "backstop step=rescue_2 verdict=STALE"
    assert call["metadata"]["rescue_step"] == "rescue_2"


async def test_ledger_is_not_part_of_the_cache_key(monkeypatch):
    fake = FakeAsyncTelem()
    monkeypatch.setattr(telem_client, "AsyncTelem", lambda **kwargs: fake)
    await telem_client.search_engines("same q", ["tavily"], goal="g", **ledger("initial", None, "tavily"))
    again = await telem_client.search_engines("same q", ["tavily"], goal="g", **ledger("rescue_1", "DEAD", "tavily"))
    plain = await telem_client.search_engines("same q", ["tavily"], goal="g")
    assert len(fake.calls) == 1
    assert again["tavily"].cached and plain["tavily"].cached


class RecordingEngines:
    """engine_search stand-in recording every kwarg it receives."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, query: str, engines: list[str], **kwargs: Any) -> dict[str, EngineRun]:
        self.calls.append({"query": query, "engines": engines, **kwargs})
        engine = engines[0]
        return {engine: EngineRun(engine=engine, status="ok", cost_usd=0.001, session_id="sess-A", results=[
            Result(rank=1, title="t", url=f"https://{engine}.example/{len(self.calls)}", engine=engine)])}


async def test_second_opinion_labels_each_step_and_reuses_one_session(monkeypatch):
    install_checker(monkeypatch, [verdict("WRONG_ENTITY"), verdict("STALE"), verdict("HIT", evidence_rank=1)])
    engines = RecordingEngines()
    ctx = RunContext(run_id="r", question_id="q", setup="second_opinion")
    outcome = await search("q", "goal", ctx, engine_search=engines)
    assert outcome.status == "HIT"
    labels = [(c["metadata"]["rescue_step"], c["metadata"]["verdict"], c["metadata"]["engine"]) for c in engines.calls]
    assert labels == [("initial", "none", "tavily"), ("rescue_1", "WRONG_ENTITY", "exa"),
                      ("rescue_2", "STALE", "perplexity")]
    assert [c["context"] for c in engines.calls][1] == "backstop step=rescue_1 verdict=WRONG_ENTITY"
    assert [c["session"] for c in engines.calls] == [None, "sess-A", "sess-A"]
    assert all(c["goal"] == "goal" for c in engines.calls)
    done = next(e for e in reversed(outcome.trace) if e["type"] == "search_done")
    assert done["data"]["telem_session_id"] == "sess-A"


async def test_engine_error_retry_is_labelled(monkeypatch):
    install_checker(monkeypatch, [verdict("DEAD"), verdict("HIT", evidence_rank=1)])
    calls: list[dict[str, Any]] = []

    async def engines(query, engines, **kwargs):
        calls.append(kwargs)
        engine = engines[0]
        if len(calls) == 1:
            return {engine: EngineRun(engine=engine, status="error", session_id="s")}
        return {engine: EngineRun(engine=engine, status="ok", session_id="s",
                                  results=[Result(rank=1, title="t", url="https://x.example", engine=engine)])}

    ctx = RunContext(run_id="r", question_id="q", setup="second_opinion")
    await search("q", "goal", ctx, engine_search=engines)
    assert calls[1]["metadata"]["rescue_step"] == "engine_retry"
    assert calls[1]["metadata"]["verdict"] == "ENGINE_ERROR"


async def test_engine_search_without_ledger_params_still_works(monkeypatch):
    install_checker(monkeypatch, [verdict("HIT", evidence_rank=1)])
    seen = []

    async def legacy(query, engines, *, num_results=5, goal=None, published_after=None, session=None):
        seen.append(session)
        return {engines[0]: EngineRun(engine=engines[0], status="ok", results=[
            Result(rank=1, title="t", url="https://x.example", engine=engines[0])])}

    ctx = RunContext(run_id="r", question_id="q", setup="second_opinion")
    assert (await search("q", "goal", ctx, engine_search=legacy)).status == "HIT"
    assert seen == [None]


async def test_backstop_client_forwards_ledger_and_fails_open(monkeypatch, caplog):
    install_checker(monkeypatch, [verdict("DEAD"), verdict("HIT", evidence_rank=1)])
    fake = FakeAsyncTelem(reject={"metadata"})
    with caplog.at_level(logging.WARNING, logger="so.ledger"):
        resp = await BackstopTelem(telem=fake).search("q", goal="g")
    assert resp.status == "HIT"
    sent = [(call.get("metadata") or {}).get("rescue_step") for call in fake.calls]
    assert sent == ["initial", None, "rescue_1", None]
    assert [call["session"] for call in fake.calls] == [None, None, "sess-new", "sess-new"]
    assert "retrying without them" in caplog.text


async def test_backstop_client_caller_metadata_wins(monkeypatch):
    install_checker(monkeypatch, [verdict("HIT", evidence_rank=1)])
    fake = FakeAsyncTelem()
    await BackstopTelem(telem=fake).search("q", goal="g", metadata={"tenant": "acme"})
    assert fake.calls[0]["metadata"] == {"tenant": "acme"}
    assert fake.calls[0]["context"].startswith("backstop step=initial")
