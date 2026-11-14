"""Checker parsing, validation and call-path tests. No network, no model."""
from __future__ import annotations

import json
from datetime import date

import pytest

from so import checker
from so.checker import CheckedVerdict, check, check_page, parse_page_verdict, parse_verdict
from so.config import BudgetExceeded
from so.contracts import CheckerVerdict, PageText, Result, RunContext
from tests.o1.examples import EXAMPLES, TODAY
from tests.o1.fakes import RecordingLog, ScriptedModel

RESULTS = [Result(rank=i, title=f"Title {i}", url=f"https://site{i}.example/page", snippet=f"Snippet {i}",
                  engine="tavily") for i in range(1, 6)]


def reply(**fields) -> str:
    base = {"verdict": "DEAD", "confidence": 0.8, "why": "w", "evidence_rank": None, "best_url": None,
            "rewrite": None}
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
    ("hit", "HIT"), ("Wrong entity", "WRONG_ENTITY"), ("wrong-entity", "WRONG_ENTITY"),
    ("WRONG", "WRONG_ENTITY"), ("not found", "DEAD"), ("outdated", "STALE"), (" shallow ", "SHALLOW"),
])
def test_normalizes_verdict_spellings(raw, expected):
    assert checker.normalize_verdict(raw) == expected


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2]", reply(verdict="MAYBE")])
def test_rejects_unusable_replies(text):
    with pytest.raises(ValueError):
        parse_verdict(text, RESULTS, "q")


@pytest.mark.parametrize("raw,expected", [(0.7, 0.7), (85, 0.85), (-1, 0.0), (3000, 1.0), ("high", 0.5)])
def test_confidence_is_clamped_to_unit_interval(raw, expected):
    out = parse_verdict(reply(verdict="DEAD", confidence=raw), RESULTS, "q")
    assert out.confidence == pytest.approx(expected)


# Rules that keep HIT trustworthy.

def test_confident_hit_points_at_its_evidence():
    out = parse_verdict(reply(verdict="HIT", confidence=0.9, evidence_rank=3, rewrite="ignored"), RESULTS, "q")
    assert (out.verdict, out.evidence_rank, out.best_url, out.rewrite) == ("HIT", 3, RESULTS[2].url, None)


def test_unsure_hit_becomes_shallow_on_its_evidence_page():
    out = parse_verdict(reply(verdict="HIT", confidence=0.4, evidence_rank=2), RESULTS, "orig query")
    assert (out.verdict, out.best_url, out.evidence_rank, out.rewrite) == ("SHALLOW", RESULTS[1].url, None,
                                                                          "orig query")
    assert out.why.startswith("Unsure hit")


@pytest.mark.parametrize("rank", [None, 0, 6, "two"])
def test_hit_without_a_valid_evidence_rank_is_not_a_hit(rank):
    out = parse_verdict(reply(verdict="HIT", confidence=0.95, evidence_rank=rank), RESULTS, "q")
    assert out.verdict == "DEAD"


def test_hit_without_rank_but_with_a_real_url_is_read():
    out = parse_verdict(reply(verdict="HIT", confidence=0.95, best_url=RESULTS[4].url), RESULTS, "q")
    assert (out.verdict, out.best_url) == ("SHALLOW", RESULTS[4].url)


def test_shallow_url_must_come_from_results():
    near = "http://www.site2.example/page/"
    out = parse_verdict(reply(verdict="SHALLOW", best_url=near), RESULTS, "q")
    assert (out.verdict, out.best_url) == ("SHALLOW", RESULTS[1].url)
    invented = parse_verdict(reply(verdict="SHALLOW", best_url="https://made-up.example/x"), RESULTS, "q")
    assert invented.verdict == "DEAD" and invented.best_url is None


def test_shallow_falls_back_to_evidence_rank_url():
    out = parse_verdict(reply(verdict="SHALLOW", evidence_rank=4), RESULTS, "q")
    assert (out.verdict, out.best_url, out.evidence_rank) == ("SHALLOW", RESULTS[3].url, None)


@pytest.mark.parametrize("verdict", ["STALE", "WRONG_ENTITY", "DEAD"])
def test_misses_always_carry_a_rewrite_and_no_evidence(verdict):
    out = parse_verdict(reply(verdict=verdict, evidence_rank=1, best_url=RESULTS[0].url, rewrite=None),
                        RESULTS, "fallback query")
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
    assert system == checker.SYSTEM_PROMPT
    assert "TODAY: 2026-11-14" in user and "QUERY: the query" in user and "GOAL: the goal" in user
    assert "[5] Title 5" in user and "Sixth" not in user


async def test_accepts_dict_results(model):
    model(reply(verdict="HIT", confidence=0.9, evidence_rank=1))
    out = await check("q", "g", [r.model_dump() for r in RESULTS], TODAY)
    assert out.verdict == "HIT"


async def test_malformed_reply_is_retried_once(model):
    fake = model("not json", reply(verdict="STALE"))
    out = await check("q", "g", RESULTS, TODAY)
    assert out.verdict == "STALE" and len(fake.calls) == 2
    assert out.cost_usd == pytest.approx(2 * fake.cost_usd)


async def test_two_malformed_replies_degrade_to_dead(model):
    model("nope", "still nope")
    out = await check("my query", "g", RESULTS, TODAY)
    assert (out.verdict, out.confidence, out.rewrite) == ("DEAD", 0.0, "my query")


async def test_model_error_degrades_to_dead(model):
    model(RuntimeError("provider down"))
    out = await check("q", "g", RESULTS, TODAY)
    assert out.verdict == "DEAD" and "RuntimeError" in out.why


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
    assert system == checker.PAGE_PROMPT and "PAGE URL: https://p.example" in user
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
