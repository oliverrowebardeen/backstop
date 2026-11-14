"""Lane S4 tests: resume detection, cost_per_search_call, paired flips, checker join."""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from data.load import load_questions
from evals.run_eval import finished_runs, run
from evals.score import checker_metrics, first_engine_checks, log_metrics, paired_flips, setup_block
from tests.s4 import fake_eventlog


def ev(type_, qid="sqa-0001", setup="single", **data):
    return {"ts": "2026-11-14T19:00:00.000Z", "run_id": "x", "question_id": qid, "setup": setup,
            "seq": 1, "type": type_, "data": data}


def write_log(path, events, torn_tail=False):
    text = "".join(json.dumps(e) + "\n" for e in events)
    if torn_tail:
        text += '{"ts": "2026-11-14T19:00:01.000Z", "type": "run_e'
    path.write_text(text)


END = dict(engine_calls=1, fetches=0, searches=1, cost_usd_search=0.005, cost_usd_llm=0.01,
           cost_usd_total=0.015, latency_ms=1000)


# ---------------------------------------------------------------- resume

def test_finished_runs_needs_run_end(tmp_path):
    write_log(tmp_path / "single-sqa-0001-aaaaaaaa.jsonl", [ev("run_start", question="q"), ev("run_end", **END)])
    # half-finished: no run_end, and a torn last line
    write_log(tmp_path / "single-sqa-0002-bbbbbbbb.jsonl", [ev("run_start", qid="sqa-0002", question="q"),
                                                            ev("search", qid="sqa-0002", engines=["tavily"])],
              torn_tail=True)
    # hyphenated ids and an underscore setup parse from the events, not the file name
    write_log(tmp_path / "second_opinion-trap-001-cccccccc.jsonl",
              [ev("run_start", qid="trap-001", setup="second_opinion", question="q"),
               ev("run_end", qid="trap-001", setup="second_opinion", **END)])
    done = finished_runs(tmp_path)
    assert set(done) == {("sqa-0001", "single"), ("trap-001", "second_opinion")}


def test_finished_runs_newest_wins(tmp_path):
    old, new = tmp_path / "single-sqa-0001-aaaaaaaa.jsonl", tmp_path / "single-sqa-0001-bbbbbbbb.jsonl"
    write_log(old, [ev("run_end", **END)])
    write_log(new, [ev("run_end", **END)])
    t = time.time()
    os.utime(old, (t - 100, t - 100))
    assert finished_runs(tmp_path)[("sqa-0001", "single")] == new


def test_run_skips_finished_and_reruns_half_finished(tmp_path):
    qs = load_questions(split="test")[:3]
    # a crashed earlier attempt for qs[0]
    write_log(tmp_path / f"single-{qs[0].id}-deadbeef.jsonl", [ev("run_start", qid=qs[0].id, question="q")])
    state = asyncio.run(run(qs, [], ["single"], tmp_path, concurrency=2))
    assert state["done"] == 3 and not state["failures"]
    assert set(finished_runs(tmp_path)) == {(q.id, "single") for q in qs}
    assert len(list(tmp_path.glob(f"single-{qs[0].id}-*.jsonl"))) == 2  # old one kept, ignored
    again = asyncio.run(run(qs, [], ["single"], tmp_path, concurrency=2))
    assert again["done"] == 0


def test_run_catches_agent_errors(tmp_path, monkeypatch):
    qs = load_questions(split="test")[:3]
    monkeypatch.setenv("FAKE_AGENT_FAIL", qs[1].id)
    state = asyncio.run(run(qs, [], ["second_opinion"], tmp_path, concurrency=3))
    assert [f[0] for f in state["failures"]] == [qs[1].id]
    assert set(finished_runs(tmp_path)) == {(qs[0].id, "second_opinion"), (qs[2].id, "second_opinion")}


# ---------------------------------------------------------------- cost_per_search_call

SO_EVENTS = [
    ev("run_start", question="q"),
    ev("llm", role="agent", cost_usd=0.010),
    # search call 1: tavily, check, SHALLOW rescue with a Tavily Extract read, re-check
    ev("search", engines=["tavily"], cost_usd=0.005),
    ev("llm", role="checker", cost_usd=0.001),
    ev("check", verdict="SHALLOW"),
    ev("rescue", action="FETCH_PAGE"),
    ev("fetch", via="tavily", cost_usd=0.002),
    ev("llm", role="checker", cost_usd=0.001),
    ev("check", verdict="HIT"),
    ev("search_done", status="HIT", calls=1, fetches=1, cost_usd=0.007),
    # the agent's own read_page: not part of a search call
    ev("fetch", via="telem", cost_usd=0.003),
    ev("llm", role="agent", cost_usd=0.010),
    # search call 2: tavily miss, retry on exa, hit
    ev("search", engines=["tavily"], cost_usd=0.005),
    ev("llm", role="checker", cost_usd=0.001),
    ev("check", verdict="DEAD"),
    ev("rescue", action="RETRY_ENGINE", engine="exa"),
    ev("search", engines=["exa"], cost_usd=0.005),
    ev("llm", role="checker", cost_usd=0.001),
    ev("check", verdict="HIT"),
    ev("search_done", status="HIT", calls=2, fetches=0, cost_usd=0.010),
    ev("llm", role="judge", cost_usd=0.050),  # must never count
    ev("answer", final="x", status="ANSWERED", citations=[]),
]


def test_cost_per_search_call_counts_search_fetch_checker_only():
    end = dict(engine_calls=3, fetches=2, searches=2, cost_usd_search=0.020,
               cost_usd_llm=0.074, cost_usd_total=0.094, latency_ms=5000)
    m = log_metrics(SO_EVENTS, end)
    # search 0.015 + in-call fetch 0.002 + checker 0.004; read_page fetch and agent/judge llm excluded
    assert m["search_call_cost"] == pytest.approx(0.021)
    assert m["search_calls"] == 2
    assert m["cost_total"] == pytest.approx(0.044)  # judge 0.050 removed
    assert m["judge_cost"] == pytest.approx(0.050)

    single = [ev("search", engines=["tavily"], cost_usd=0.005), ev("search_done", status="UNCHECKED"),
              ev("search", engines=["tavily"], cost_usd=0.005), ev("search_done", status="UNCHECKED")]
    s = log_metrics(single, dict(END, engine_calls=2, searches=2, cost_usd_search=0.01))
    block = setup_block([dict(m, grade="correct"), dict(s, grade="wrong")], [])
    assert block["search_calls"] == 4
    assert block["cost_per_search_call"] == pytest.approx((0.021 + 0.010) / 4)
    assert block["cost_per_correct"] == pytest.approx((0.044 + 0.015) / 1)
    assert block["wrong_rate"] == 0.5 and block["accuracy"] == 0.5


def test_fake_agent_log_totals_are_consistent(tmp_path):
    q = load_questions(split="test")[0]
    from tests.s4.fake_agent import run_question
    for setup in ("single", "telem_default", "second_opinion"):
        asyncio.run(run_question(q, setup, log_dir=tmp_path))
    for path in tmp_path.glob("*.jsonl"):
        events = fake_eventlog.read_log(path)
        end = fake_eventlog.summarize(path)
        llm = sum(e["data"]["cost_usd"] for e in events if e["type"] == "llm")
        search = sum(e["data"]["cost_usd"] for e in events if e["type"] in ("search", "fetch"))
        assert end["cost_usd_total"] == pytest.approx(llm + search, abs=1e-5)
        assert end["engine_calls"] == sum(len(e["data"]["engines"]) for e in events if e["type"] == "search")
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        try:
            from so.contracts import Event
        except ImportError:
            continue
        for e in events:
            Event.model_validate(e)


# ---------------------------------------------------------------- paired flips

def test_paired_flips():
    base = {"a": "wrong", "b": "correct", "c": "declined", "d": "wrong", "e": "correct", "only_base": "wrong"}
    ours = {"a": "correct", "b": "wrong", "c": "correct", "d": "declined", "e": "correct", "only_ours": "correct"}
    p = paired_flips(ours, base)
    assert p["n_both"] == 5
    assert p["wrong->correct"] == 1
    assert p["correct->wrong"] == 1
    assert p["declined->correct"] == 1
    assert p["wrong->declined"] == 1
    assert p["correct->declined"] == 0 and p["declined->wrong"] == 0
    assert p["unchanged"] == 1


# ---------------------------------------------------------------- checker join

def test_first_engine_checks_and_metrics():
    assert first_engine_checks(SO_EVENTS) == ["SHALLOW", "DEAD"]  # the exa re-check is not a first check
    matrix = {("q1", "tavily"): False, ("q1", "exa"): True,     # rescuable miss
              ("q2", "tavily"): False, ("q2", "exa"): False,    # dead miss
              ("q3", "tavily"): True, ("q4", "tavily"): True}
    joined = [("q1", "WRONG_ENTITY"), ("q2", "DEAD"), ("q3", "HIT"), ("q4", "SHALLOW"), ("q9", "HIT")]
    c = checker_metrics(joined, matrix, ["tavily", "exa"])
    assert c["n_searches"] == 4 and c["counts"]["unjoined"] == 1
    assert c["miss_recall"] == 1.0
    assert c["false_alarm_rate"] == 0.5
    assert c["rescuable_vs_dead_acc"] == 1.0
