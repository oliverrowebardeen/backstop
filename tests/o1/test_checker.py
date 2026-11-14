"""Checker parsing, validation and call-path tests. No network, no model."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from so import checker
from so.checker import (MALFORMED_WHY, TRUNCATED_WHY, CheckedVerdict, CheckerError, check, check_page,
                        parse_page_verdict, parse_verdict)
from so import config
from so.config import BudgetExceeded
from so.contracts import CheckerVerdict, PageText, Result, RunContext
from tests.o1.examples import EXAMPLES, TODAY
from tests.o1.fakes import RecordingLog, ScriptedModel

RESULTS = [Result(rank=i, title=f"Title {i}", url=f"https://site{i}.example/page", snippet=f"Snippet {i}",
                  engine="tavily") for i in range(1, 6)]


def reply(**fields) -> str:
    base = {"verdict": "DEAD", "confidence": 0.8, "why": "w", "rank": None, "rewrite": None}
    return json.dumps({**base, **fields})


@pytest.fixture
def model(monkeypatch):
    def install(*replies):
        fake = ScriptedModel(*replies)
        monkeypatch.setattr(checker, "_call_model", fake)
        return fake
    return install


# The fifteen hand-made examples, through the full call path.

@pytest.mark.parametrize("ex", EXAMPLES, ids=[e["id"] for e in EXAMPLES])
async def test_examples_give_expected_verdict(model, ex):
    fake = model(ex["reply"]) if ex["reply"] else model()
    out = await check(ex["query"], ex["goal"], ex["results"], TODAY)
    assert isinstance(out, CheckerVerdict)
    assert out.verdict == ex["expected"]
    urls = {r["url"] for r in ex["results"]}
    if out.verdict == "HIT":
        assert out.evidence_rank is not None and out.best_url in urls and out.rewrite is None
    elif out.verdict == "SHALLOW":
        assert out.best_url in urls and out.evidence_rank is None
    else:
        assert out.evidence_rank is None and out.rewrite
    assert len(fake.calls) == (1 if ex["results"] else 0)


def test_examples_cover_each_verdict_three_times():
    counts = {v: sum(e["expected"] == v for e in EXAMPLES) for v in checker.VERDICTS}
    assert counts == {v: 3 for v in checker.VERDICTS}


# Parsing.

def test_parses_code_fenced_and_wrapped_json():
    fenced = "```json\n" + reply(verdict="DEAD", rewrite="x") + "\n```"
    assert parse_verdict(fenced, RESULTS, "q").verdict == "DEAD"
    wrapped = "Here is my verdict: " + reply(verdict="STALE") + " Thanks."
    assert parse_verdict(wrapped, RESULTS, "q").verdict == "STALE"


@pytest.mark.parametrize("raw,expected", [
    ("hit", "HIT"), ("Wrong entity", "WRONG_ENTITY"), ("wrong-entity", "WRONG_ENTITY"), (" shallow ", "SHALLOW"),
])
def test_normalizes_verdict_spellings(raw, expected):
    assert checker.normalize_verdict(raw) == expected


@pytest.mark.parametrize("raw", ["WRONG", "not found", "outdated", "MAYBE", None, 3])
def test_rejects_anything_but_the_five_verdicts(raw):
    with pytest.raises(ValueError):
        checker.normalize_verdict(raw)


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2]", reply(verdict="MAYBE"),
                                  json.dumps({"verdict": "DEAD", "confidence": 0.8}),
                                  json.dumps({"verdict": "DEAD", "why": "w"})])
def test_rejects_unusable_replies(text):
    with pytest.raises(ValueError):
        parse_verdict(text, RESULTS, "q")


@pytest.mark.parametrize("raw", [0, 0.7, 1])
def test_accepts_confidence_in_unit_interval(raw):
    assert parse_verdict(reply(confidence=raw), RESULTS, "q").confidence == raw


@pytest.mark.parametrize("raw", [85, -0.1, 1.01, "high", None, True])
def test_confidence_outside_unit_interval_is_malformed(raw):
    with pytest.raises(ValueError):
        parse_verdict(reply(confidence=raw), RESULTS, "q")


def test_hit_threshold_setting_is_validated(monkeypatch):
    monkeypatch.setenv("CHECKER_TEST_THRESHOLD", "1.5")
    with pytest.raises(ValueError):
        config._unit_interval("CHECKER_TEST_THRESHOLD", "0.6")
    monkeypatch.setenv("CHECKER_TEST_THRESHOLD", "0.7")
    assert config._unit_interval("CHECKER_TEST_THRESHOLD", "0.6") == 0.7


def _config_in_clean_env(**env: str) -> dict:
    """Import so.config in a fresh interpreter with no .env file and only the given settings."""
    code = ("import json; from so import config as c; print(json.dumps([c.CHECKER_HIT_MIN_CONFIDENCE, "
            "c.CHECKER_THINKING, c.CHECKER_MAX_TOKENS, c.LLM_MAX_TOKENS['checker'], c.LLM_THINKING['checker']]))")
    base = {"PATH": os.environ.get("PATH", ""), "SO_ENV_FILE": os.devnull}
    proc = subprocess.run([sys.executable, "-c", code], env={**base, **env}, capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[2])
    return {"returncode": proc.returncode, "values": json.loads(proc.stdout) if proc.returncode == 0 else None,
            "stderr": proc.stderr}


def test_checker_settings_defaults_and_wiring():
    assert _config_in_clean_env()["values"] == [0.6, "disabled", 399, 399, "disabled"]
    custom = _config_in_clean_env(CHECKER_HIT_MIN_CONFIDENCE="0.7", CHECKER_THINKING="adaptive",
                                  CHECKER_MAX_TOKENS="2000")
    assert custom["values"] == [0.7, "adaptive", 2000, 2000, "adaptive"]


@pytest.mark.parametrize("env", [{"CHECKER_HIT_MIN_CONFIDENCE": "1.5"}, {"CHECKER_THINKING": "on"}])
def test_invalid_checker_settings_fail_at_import(env):
    out = _config_in_clean_env(**env)
    assert out["returncode"] != 0 and "ValueError" in out["stderr"]


def test_hit_floor_is_read_from_config(monkeypatch):
    monkeypatch.setattr(config, "CHECKER_HIT_MIN_CONFIDENCE", 0.95)
    out = parse_verdict(reply(verdict="HIT", confidence=0.9, rank=1), RESULTS, "q")
    assert (out.verdict, out.best_url) == ("SHALLOW", RESULTS[0].url)


# Rules that keep HIT trustworthy.

def test_confident_hit_points_at_its_evidence():
    out = parse_verdict(reply(verdict="HIT", confidence=0.9, rank=3, rewrite="ignored"), RESULTS, "q")
    assert (out.verdict, out.evidence_rank, out.best_url, out.rewrite) == ("HIT", 3, RESULTS[2].url, None)


def test_unsure_hit_becomes_shallow_on_its_evidence_page():
    out = parse_verdict(reply(verdict="HIT", confidence=0.4, rank=2), RESULTS, "orig query")
    assert (out.verdict, out.best_url, out.evidence_rank, out.rewrite) == ("SHALLOW", RESULTS[1].url, None,
                                                                          "orig query")
    assert out.why.startswith("Unsure hit")


@pytest.mark.parametrize("rank", [None, 0, 6, "two"])
def test_hit_without_a_valid_evidence_rank_is_not_a_hit(rank):
    out = parse_verdict(reply(verdict="HIT", confidence=0.95, rank=rank), RESULTS, "q")
    assert out.verdict == "DEAD" and out.why.startswith("Unsure hit (no valid result number)")


def test_shallow_maps_its_result_number_to_that_url():
    out = parse_verdict(reply(verdict="SHALLOW", rank=4), RESULTS, "q")
    assert (out.verdict, out.best_url, out.evidence_rank) == ("SHALLOW", RESULTS[3].url, None)


@pytest.mark.parametrize("rank", [None, 0, 6])
def test_shallow_without_a_valid_result_number_is_dead(rank):
    out = parse_verdict(reply(verdict="SHALLOW", rank=rank), RESULTS, "q")
    assert out.verdict == "DEAD" and out.best_url is None


@pytest.mark.parametrize("verdict", ["STALE", "WRONG_ENTITY", "DEAD"])
def test_misses_always_carry_a_rewrite_and_no_evidence(verdict):
    out = parse_verdict(reply(verdict=verdict, rank=1, rewrite=None), RESULTS, "fallback query")
    assert (out.evidence_rank, out.best_url, out.rewrite) == (None, None, "fallback query")
    kept = parse_verdict(reply(verdict=verdict, rewrite="  better   query "), RESULTS, "q")
    assert kept.rewrite == "better query"


def test_why_is_one_line_and_bounded():
    out = parse_verdict(reply(why="line one\nline two " + "x" * 1000), RESULTS, "q")
    assert "\n" not in out.why and len(out.why) <= checker.WHY_CHARS


# The call path.

async def test_prompt_has_query_goal_date_and_top_five_only(model):
    fake = model(reply())
    many = RESULTS + [Result(rank=6, title="Sixth", url="https://six.example", engine="exa")]
    await check("the query", "the goal", many, date(2026, 11, 14))
    system, user = fake.calls[0]
    assert system == checker.SYSTEM_PROMPT and fake.schemas[0] == checker.VERDICT_SCHEMA
    assert "TODAY: 2026-11-14" in user and "QUERY: the query" in user and "GOAL: the goal" in user
    assert "[5] Title 5" in user and "Sixth" not in user


async def test_accepts_dict_results(model):
    model(reply(verdict="HIT", confidence=0.9, rank=1))
    out = await check("q", "g", [r.model_dump() for r in RESULTS], TODAY)
    assert out.verdict == "HIT"


async def test_malformed_reply_is_retried_once(model):
    fake = model("not json", reply(verdict="STALE"))
    out = await check("q", "g", RESULTS, TODAY)
    assert out.verdict == "STALE" and len(fake.calls) == 2
    assert out.cost_usd == pytest.approx(2 * fake.cost_usd)


async def test_two_malformed_replies_give_a_logged_non_hit(model, caplog):
    fake = model("nope", reply(verdict="HIT", confidence=7))
    log = RecordingLog()
    ctx = RunContext(run_id="r", question_id="x", setup="second_opinion", log=log)
    out = await check("my query", "g", RESULTS, TODAY, ctx=ctx)
    assert (out.verdict, out.confidence, out.why, out.rewrite) == ("DEAD", 0.0, MALFORMED_WHY, "my query")
    assert out.cost_usd == pytest.approx(2 * fake.cost_usd)
    assert log.events[0][1]["cost_usd"] == pytest.approx(2 * fake.cost_usd)
    assert MALFORMED_WHY in caplog.text


async def test_truncated_reply_is_retried_once_with_a_different_request(model):
    fake = model(("", "max_tokens"), reply(verdict="HIT", confidence=0.9, rank=1))
    out = await check("q", "g", RESULTS, TODAY)
    assert out.verdict == "HIT" and len(fake.calls) == 2
    first, second = fake.calls
    assert second[1] == first[1] + checker.RETRY_NOTE


@pytest.mark.parametrize("stop", ["max_tokens", "length"])
async def test_two_truncated_replies_give_a_logged_non_hit(model, caplog, stop):
    complete_looking = reply(verdict="HIT", confidence=0.9, rank=1)
    fake = model(("", stop), (complete_looking, stop))
    log = RecordingLog()
    ctx = RunContext(run_id="r", question_id="x", setup="second_opinion", log=log)
    out = await check("my query", "g", RESULTS, TODAY, ctx=ctx)
    assert (out.verdict, out.why, out.rewrite) == ("DEAD", TRUNCATED_WHY, "my query")
    assert len(fake.calls) == 2 and log.events[0][1]["cost_usd"] == pytest.approx(2 * fake.cost_usd)
    assert TRUNCATED_WHY in caplog.text


async def test_model_error_raises_a_clear_checker_error(model):
    model(RuntimeError("provider down"))
    with pytest.raises(CheckerError, match="provider down"):
        await check("q", "g", RESULTS, TODAY)


async def test_error_after_a_malformed_reply_still_logs_spent_usage(model):
    fake = model("nope", RuntimeError("provider down"))
    log = RecordingLog()
    ctx = RunContext(run_id="r", question_id="x", setup="second_opinion", log=log)
    with pytest.raises(CheckerError):
        await check("q", "g", RESULTS, TODAY, ctx=ctx)
    assert [e[1]["cost_usd"] for e in log.events] == [fake.cost_usd]


async def test_budget_errors_propagate(model):
    model(BudgetExceeded("out of money"))
    with pytest.raises(BudgetExceeded):
        await check("q", "g", RESULTS, TODAY)


async def test_usage_is_carried_but_not_serialized(model):
    fake = model(reply(verdict="DEAD"))
    out = await check("q", "g", RESULTS, TODAY)
    assert isinstance(out, CheckedVerdict)
    assert (out.cost_usd, out.tokens_in, out.model) == (fake.cost_usd, 900, "fake-checker")
    assert set(out.model_dump()) == set(CheckerVerdict.model_fields)
    CheckerVerdict.model_validate_json(out.model_dump_json())


async def test_logs_checker_llm_event_when_given_ctx(model):
    model(reply())
    log = RecordingLog()
    ctx = RunContext(run_id="second_opinion-x-1", question_id="x", setup="second_opinion", log=log)
    await check("q", "g", RESULTS, TODAY, ctx=ctx)
    assert log.events == [("llm", {"role": "checker", "model": "fake-checker", "tokens_in": 900,
                                   "tokens_out": 60, "cost_usd": 0.0002, "latency_ms": 420})]


async def test_no_results_skips_the_model(model):
    fake = model()
    log = RecordingLog()
    ctx = RunContext(run_id="r", question_id="x", setup="second_opinion", log=log)
    out = await check("q", "g", [], TODAY, ctx=ctx)
    assert out.verdict == "DEAD" and out.cost_usd == 0 and not fake.calls and not log.events


# Page checks on the SHALLOW path.

def test_page_hit_cites_the_page():
    out = parse_page_verdict(reply(verdict="HIT", confidence=0.9), "https://p.example", "q")
    assert (out.verdict, out.best_url, out.evidence_rank, out.rewrite) == ("HIT", "https://p.example", None, None)


@pytest.mark.parametrize("fields", [{"verdict": "HIT", "confidence": 0.3}, {"verdict": "SHALLOW"}])
def test_page_check_never_returns_shallow_or_unsure_hit(fields):
    out = parse_page_verdict(reply(**fields), "https://p.example", "q")
    assert out.verdict == "DEAD" and out.rewrite == "q"


async def test_check_page_accepts_pagetext_and_truncates(model):
    fake = model(reply(verdict="HIT", confidence=0.9))
    page = PageText(url="https://p.example", ok=True, text="answer " * 5000)
    out = await check_page("q", "g", page, TODAY)
    system, user = fake.calls[0]
    assert system == checker.PAGE_PROMPT and fake.schemas[0] == checker.PAGE_VERDICT_SCHEMA
    assert "PAGE URL: https://p.example" in user
    assert user.count("answer") <= checker.PAGE_CHARS // len("answer ") + 1
    assert out.verdict == "HIT" and out.best_url == "https://p.example"


@pytest.mark.parametrize("page", [PageText(url="https://p.example", ok=False, text="error page"), "   "])
async def test_unreadable_page_is_dead_without_a_call(model, page):
    fake = model()
    out = await check_page("q", "g", page, TODAY, url="https://p.example")
    assert out.verdict == "DEAD" and not fake.calls


def test_checker_never_takes_gold():
    import inspect
    for fn in (check, check_page):
        assert "gold" not in inspect.signature(fn).parameters
