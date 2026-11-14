"""Story rules, candidate search and live-pair recording for the demo, on synthetic logs."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from data.load import load_questions, load_traps
from demo import candidates, record, stories
from demo.stories import RunView
from so import config
from so.contracts import AgentAnswer, Question
from so.eventlog import EventLog, read_log
from server.app import replay_catalog

URL = "https://example.com/evidence"

Step = tuple[str, dict[str, Any]]


def search(engine: str, verdict: str) -> list[Step]:
    return [("search", {"query": "q", "goal": "g", "engines": [engine], "n_results": 5,
                        "latency_ms": 100, "cost_usd": 0.005, "cached": False}),
            ("check", {"verdict": verdict, "confidence": 0.9, "why": "w", "evidence_rank": None})]


def retry(engine: str, counted: bool = True) -> list[Step]:
    return [("rescue", {"action": "RETRY_ENGINE", "engine": engine, "query": "q", "reason": "r",
                        "counted": counted})]


def fetch(verdict: str) -> list[Step]:
    return [("rescue", {"action": "FETCH_PAGE", "engine": None, "query": "q", "reason": "r", "url": URL}),
            ("fetch", {"url": URL, "ok": True, "via": "tavily", "latency_ms": 300, "cost_usd": 0.0016}),
            ("check", {"verdict": verdict, "confidence": 0.9, "why": "w", "evidence_rank": None})]


def done(status: str) -> list[Step]:
    return [("search_done", {"status": status, "calls": 1, "fetches": 0, "cost_usd": 0.005,
                             "latency_ms": 100, "note": ""})]


def answer(status: str = "ANSWERED", final: str = "Ada is the CEO.") -> list[Step]:
    final = "NOT FOUND" if status == "NOT_FOUND" else final
    return [("answer", {"final": final, "status": status, "citations": [] if status == "NOT_FOUND" else [URL]})]


def write_run(log_dir: Path, question: Question, setup: str, steps: list[Step]) -> Path:
    log = EventLog(f"{setup}-{question.id}-abc123", question.id, setup, log_dir=log_dir)
    log.emit("run_start", question=question.question)
    for kind, data in steps:
        log.emit(kind, **data)
    log.emit("run_end", engine_calls=2, fetches=0, searches=1, cost_usd_search=0.01, cost_usd_llm=0.02,
             cost_usd_total=0.03, latency_ms=4000)
    return log.path


def events(steps: list[Step]) -> list[dict[str, Any]]:
    return [{"type": kind, "data": data} for kind, data in steps]


@pytest.fixture(autouse=True)
def llm_checker(monkeypatch):
    """Recordings use the configured checker backend; tests start from the code default."""
    monkeypatch.setattr(config, "CHECKER_BACKEND", "llm")


@pytest.fixture
def fact() -> Question:
    return load_questions(split="test", category="fact")[0]


@pytest.fixture
def trap() -> Question:
    return load_traps()[0]


def test_counted_retry_followed_by_hit_is_a_rescue():
    path = search("tavily", "DEAD") + retry("exa") + search("exa", "HIT") + done("HIT")
    assert stories.rescued_by(events(path), "RETRY_ENGINE")
    assert not stories.rescued_by(events(path), "FETCH_PAGE")


def test_engine_error_retry_is_not_a_rescue():
    path = search("tavily", "DEAD") + retry("exa", counted=False) + search("exa", "HIT") + done("HIT")
    assert not stories.rescued_by(events(path), "RETRY_ENGINE")


def test_hit_in_a_later_search_call_does_not_credit_an_earlier_rescue():
    path = (search("tavily", "DEAD") + retry("exa") + search("exa", "DEAD") + done("NOT_FOUND")
            + search("tavily", "HIT") + done("HIT"))
    assert not stories.rescued_by(events(path), "RETRY_ENGINE")


def test_search_path_reads_as_one_line_per_search_call():
    path = search("tavily", "SHALLOW") + fetch("HIT") + done("HIT") + search("tavily", "HIT") + done("HIT")
    assert stories.search_path(events(path)) == ["tavily SHALLOW > read via tavily HIT = HIT", "tavily HIT = HIT"]


def test_stories_for_each_kind(fact, trap):
    wrong = RunView.from_events("single", events(answer()), grade="wrong")
    retried = RunView.from_events("second_opinion", events(
        search("tavily", "WRONG_ENTITY") + retry("exa") + search("exa", "HIT") + done("HIT") + answer()),
        grade="correct")
    fetched = RunView.from_events("second_opinion", events(
        search("tavily", "SHALLOW") + fetch("HIT") + done("HIT") + answer()), grade="correct")
    declined = RunView.from_events("second_opinion", events(answer("NOT_FOUND")), grade="declined")
    assert stories.stories_for(fact, wrong, retried) == ["retry_recovery"]
    assert stories.stories_for(fact, wrong, fetched) == ["shallow_fetch"]
    assert stories.stories_for(trap, wrong, declined) == ["trap_decline"]
    assert stories.stories_for(trap, wrong, retried) == []
    right = RunView.from_events("single", events(answer()), grade="correct")
    assert stories.stories_for(fact, right, retried) == []


async def test_candidates_use_judge_grades_and_skip_unpaired_runs(tmp_path, fact, trap):
    write_run(tmp_path, fact, "single", answer(final="Bob is the CEO."))
    write_run(tmp_path, fact, "second_opinion",
              search("tavily", "DEAD") + retry("exa") + search("exa", "HIT") + done("HIT") + answer())
    write_run(tmp_path, trap, "single", answer(final="Bob is the CEO."))
    write_run(tmp_path, trap, "second_opinion", answer("NOT_FOUND"))
    learn = load_questions(split="learn")[0]
    write_run(tmp_path, learn, "single", answer())
    write_run(tmp_path, learn, "second_opinion", search("tavily", "SHALLOW") + fetch("HIT") + done("HIT") + answer())
    graded: list[str] = []

    async def grade(question: Question, reply: dict[str, Any]) -> str:
        graded.append(question.id)
        if reply["status"] == "NOT_FOUND":
            return "declined"
        return "wrong" if reply["final"].startswith("Bob") else "correct"

    found = await candidates.find_candidates(tmp_path, grade=grade)
    assert [c.question_id for c in found["retry_recovery"]] == [fact.id]
    assert [c.question_id for c in found["trap_decline"]] == [trap.id]
    assert found["shallow_fetch"] == []
    assert learn.id not in graded
    entries = candidates.golden_entries(found)
    assert [e["name"] for e in entries] == ["retry-recovery", "trap-decline"]
    assert entries[0]["eval_evidence"]["second_opinion"]["path"] == ["tavily DEAD > exa HIT = HIT"]
    assert not {"left", "right"} & json.loads(json.dumps({"golden": entries})).keys()


async def test_second_opinion_runs_can_come_from_a_separate_pass(tmp_path, fact):
    main, extra = tmp_path / "logs", tmp_path / "logs_cascade"
    write_run(main, fact, "single", answer(final="Bob is the CEO."))
    write_run(main, fact, "second_opinion", search("tavily", "SHALLOW") + fetch("HIT") + done("HIT") + answer())
    write_run(extra, fact, "single", answer())
    write_run(extra, fact, "second_opinion",
              search("tavily", "DEAD") + retry("exa") + search("exa", "HIT") + done("HIT") + answer())

    async def grade(question: Question, reply: dict[str, Any]) -> str:
        return "wrong" if reply["final"].startswith("Bob") else "correct"

    found = await candidates.find_candidates(main, grade=grade, so_log_dir=extra)
    assert [c.question_id for c in found["retry_recovery"]] == [fact.id]
    assert found["shallow_fetch"] == []
    same_pass = await candidates.find_candidates(main, grade=grade)
    assert [c.question_id for c in same_pass["shallow_fetch"]] == [fact.id]

    golden = tmp_path / "golden.json"
    candidates.write_golden(candidates.golden_entries(found), golden,
                            log_dirs={"single": main, "second_opinion": config.ROOT / "out" / "logs_cascade"})
    assert json.loads(golden.read_text())["log_dirs"] == {"single": "logs", "second_opinion": "out/logs_cascade"}


@dataclass
class LiveRuns:
    seen: list[tuple[str, str]] = field(default_factory=list)
    decisions: bool = True


@pytest.fixture
def live_runs(monkeypatch):
    """Replace the agent with one that writes a complete log and records cache mode and checker backend."""
    state = LiveRuns()

    async def run_question(question: Question, setup: str, *, log_dir: Path) -> AgentAnswer:
        state.seen.append((config.SO_CACHE, config.CHECKER_BACKEND))
        steps = answer()
        if setup == "second_opinion":
            checker_model = (config.DECISION_MODEL if config.CHECKER_BACKEND != "llm" and state.decisions
                             else config.CHECKER_MODEL)
            steps = ([("llm", {"role": "checker", "model": checker_model, "tokens_in": 10, "tokens_out": 2,
                               "cost_usd": 0.0001, "latency_ms": 300, "cached": False})]
                     + search("tavily", "DEAD") + retry("exa") + search("exa", "HIT") + done("HIT") + answer())
        write_run(Path(log_dir), question, setup, steps)
        return AgentAnswer(final="Ada is the CEO.", status="ANSWERED", citations=[URL])

    monkeypatch.setattr("so.agent.run_question", run_question)
    return state


async def test_record_writes_a_pair_the_server_can_replay(tmp_path, fact, live_runs):
    spec = record.PairSpec("retry-recovery", fact, story="retry_recovery")
    cache_mode = config.SO_CACHE
    outcome = await record.record(spec, tmp_path)
    assert live_runs.seen == [("off", "llm"), ("off", "llm")] and config.SO_CACHE == cache_mode
    manifest = json.loads((tmp_path / "retry-recovery.json").read_text())
    assert manifest["left"] == "recordings/retry-recovery/left.jsonl" and manifest["live"] is True
    assert manifest["checker_backend"] == "llm" and "checker_cascade_threshold" not in manifest
    assert "fixture" not in manifest
    pair = replay_catalog(tmp_path, None)["retry-recovery"]
    assert not pair.fixture and pair.question == fact.question
    assert outcome["story_holds"] is True
    assert read_log(tmp_path / manifest["right"])[0]["setup"] == "second_opinion"


async def test_recording_runs_the_requested_checker_backend(tmp_path, fact, live_runs, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    outcome = await record.record(record.PairSpec("cascade", fact), tmp_path, checker_backend="cascade")
    assert live_runs.seen == [("off", "cascade"), ("off", "cascade")] and config.CHECKER_BACKEND == "llm"
    manifest = json.loads((tmp_path / "cascade.json").read_text())
    assert manifest["checker_backend"] == "cascade" == outcome["checker_backend"]
    assert manifest["checker_cascade_threshold"] == config.CHECKER_CASCADE_THRESHOLD


async def test_decisions_backend_without_a_key_is_refused_before_any_live_run(tmp_path, fact, live_runs,
                                                                               monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(record.RecordingError, match="OPENAI_API_KEY"):
        await record.record(record.PairSpec("cascade", fact), tmp_path, checker_backend="cascade")
    assert live_runs.seen == []
    with pytest.raises(record.RecordingError, match="Unknown checker backend"):
        await record.record(record.PairSpec("other", fact), tmp_path, checker_backend="decisions")


async def test_silent_fallback_to_the_llm_checker_is_not_saved(tmp_path, fact, live_runs, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    live_runs.decisions = False
    with pytest.raises(record.RecordingError, match="did not run the cascade checker"):
        await record.record(record.PairSpec("fallback", fact), tmp_path, checker_backend="cascade")
    assert list(tmp_path.iterdir()) == [] and config.CHECKER_BACKEND == "llm"


async def test_existing_recording_is_refused_before_any_live_run(tmp_path, fact, live_runs):
    (tmp_path / "taken.json").write_text("{}")
    with pytest.raises(record.RecordingError, match="already exists"):
        await record.record(record.PairSpec("taken", fact), tmp_path)
    assert live_runs.seen == []


async def test_failed_run_writes_nothing(tmp_path, fact, monkeypatch):
    async def broken(question, setup, *, log_dir):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("so.agent.run_question", broken)
    with pytest.raises(record.RecordingError, match="model unavailable"):
        await record.record(record.PairSpec("broken", fact), tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_pair_that_does_not_load_is_removed(tmp_path, fact, monkeypatch):
    async def truncated(question, setup, *, log_dir):
        log = EventLog(f"{setup}-{question.id}-abc", question.id, setup, log_dir=log_dir)
        log.emit("run_start", question=question.question)

    monkeypatch.setattr("so.agent.run_question", truncated)
    with pytest.raises(record.RecordingError, match="did not load"):
        await record.record(record.PairSpec("truncated", fact), tmp_path)
    assert not (tmp_path / "truncated.json").exists()
    assert not (tmp_path / "recordings" / "truncated").exists()


def test_unsafe_names_and_unknown_setups_are_rejected(fact):
    with pytest.raises(record.RecordingError, match="URL-safe"):
        record._validate_spec(record.PairSpec("../escape", fact))
    with pytest.raises(record.RecordingError, match="Unknown setup"):
        record._validate_spec(record.PairSpec("ok", fact, left="telem"))


def test_golden_specs_resolve_question_ids(tmp_path, trap):
    path = tmp_path / "golden.json"
    path.write_text(json.dumps({"golden": [{"name": "trap-decline", "story": "trap_decline",
                                            "question_id": trap.id, "left": "single",
                                            "right": "second_opinion"}]}))
    [spec] = record.golden_specs(path)
    assert spec.question == trap and spec.story == "trap_decline"
    path.write_text(json.dumps({"golden": [{"name": "x", "question_id": "no-such-id"}]}))
    with pytest.raises(record.RecordingError, match="Unknown question id"):
        record.golden_specs(path)
