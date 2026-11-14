"""checker_eval: split guard, joining, metrics and report. No network, no model."""
from __future__ import annotations

import json

import pytest

from so import checker, checker_eval
from so.checker_eval import Case, Scored, SplitRefused, metrics
from so.contracts import CheckerVerdict, Question, Result

TODAY = "2026-11-14"


def q(qid: str, split: str = "learn", category: str = "fact") -> Question:
    return Question(id=qid, question=f"question {qid}", gold=["secret gold"], category=category,
                    source="hand", split=split)


def results(qid: str, engine: str, marker: str = "") -> list[dict]:
    return [{"rank": i, "title": f"{qid} {engine} {i}", "url": f"https://{engine}.example/{qid}/{i}",
             "snippet": f"snippet {marker}" if i == 1 else "snippet", "published": None, "engine": engine}
            for i in range(1, 6)]


def write_jsonl(path, rows) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class KeywordModel:
    """Says HIT when the prompt contains ANSWER_HERE, DEAD otherwise. Fails if gold leaks into a prompt."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def __call__(self, system: str, user: str, schema=None):
        from so.contracts import LLMResponse

        self.prompts.append(user)
        assert "secret gold" not in user
        hit = "ANSWER_HERE" in user
        body = {"verdict": "HIT" if hit else "DEAD", "confidence": 0.9, "why": "w", "rank": 1 if hit else None,
                "rewrite": None if hit else "r"}
        return LLMResponse(text=json.dumps(body), cost_usd=0.0005, latency_ms=300, model="fake-checker")


@pytest.fixture
def files(tmp_path):
    """Learn q1 (tavily hit), learn q2 (tavily miss, exa hit), learn q3 (all miss), test t1 (tavily hit)."""
    matrix = tmp_path / "matrix.jsonl"
    stored = tmp_path / "matrix_results.jsonl"
    rows = [("q1", "tavily", True), ("q1", "exa", True), ("q2", "tavily", False), ("q2", "exa", True),
            ("q3", "tavily", False), ("q3", "exa", False), ("t1", "tavily", True)]
    write_jsonl(matrix, [{"question_id": a, "engine": e, "hit": h, "hit_rank": 1 if h else None} for a, e, h in rows])
    write_jsonl(stored, [{"question_id": a, "engine": e, "results": results(a, e, "ANSWER_HERE" if h else "")}
                         for a, e, h in rows])
    with stored.open("a") as f:
        f.write('{"question_id": "q3", "engine"')  # an interrupted write
    return matrix, stored


@pytest.fixture
def model(monkeypatch):
    fake = KeywordModel()
    monkeypatch.setattr(checker, "_call_model", fake)
    return fake


QUESTIONS = [q("q1"), q("q2"), q("q3", category="finance")]


@pytest.mark.parametrize("split", ["test", "TEST", "all", ""])
def test_cli_refuses_any_split_but_learn(split, capsys):
    with pytest.raises(SystemExit) as exc:
        checker_eval.main(["--split", split])
    assert exc.value.code == 2
    assert "learn split only" in capsys.readouterr().err


async def test_run_refuses_test_split(files, model):
    matrix, stored = files
    with pytest.raises(SplitRefused):
        await checker_eval.run("test", engines=["tavily"], today=TODAY, matrix=matrix, results_path=stored,
                               questions=QUESTIONS)
    assert not model.prompts


@pytest.mark.parametrize("bad", [q("t1", split="test"),
                                 Question(id="trap-1", question="x", gold=["y"], category="trap",
                                          source="hand", split="learn", is_trap=True)])
def test_learn_questions_rejects_leaked_rows(bad):
    with pytest.raises(SplitRefused):
        checker_eval.learn_questions(QUESTIONS + [bad])


def test_real_learn_split_loads_and_passes_the_guard():
    qs = checker_eval.learn_questions()
    assert len(qs) == 100 and {x.split for x in qs} == {"learn"}


async def test_end_to_end_on_tavily(files, model, capsys):
    matrix, stored = files
    m = await checker_eval.run("learn", engines=["tavily"], today=TODAY, matrix=matrix, results_path=stored,
                               questions=QUESTIONS, show=5)
    assert (m["n_searches"], m["true_hits"], m["true_misses"]) == (3, 1, 2)
    assert (m["miss_recall"], m["false_alarm_rate"]) == (1.0, 0.0)
    assert m["confusion"]["miss"]["DEAD"] == 2 and m["confusion"]["hit"]["HIT"] == 1
    assert m["cost_usd"] == pytest.approx(0.0015)
    assert not any("t1" in p for p in model.prompts)
    out = capsys.readouterr().out
    assert "miss recall            1.000" in out and "target MET" in out


async def test_more_engines_add_cases(files, model):
    matrix, stored = files
    m = await checker_eval.run("learn", engines=["tavily", "exa"], today=TODAY, matrix=matrix,
                               results_path=stored, questions=QUESTIONS)
    assert m["n_searches"] == 6


async def test_rows_without_results_are_skipped_and_reported(files, model, capsys):
    matrix, stored = files
    rows = [json.loads(line) for line in stored.read_text().splitlines()[:-1]]
    write_jsonl(stored, [r for r in rows if r["question_id"] != "q2"])
    m = await checker_eval.run("learn", engines=["tavily"], today=TODAY, matrix=matrix, results_path=stored,
                               questions=QUESTIONS)
    assert m["n_searches"] == 2 and m["skipped_no_results"] == 1
    assert "--fill-missing" in capsys.readouterr().err


async def test_fill_missing_uses_the_matrix_call(files, model, monkeypatch):
    from so import telem_client
    from so.contracts import EngineRun

    matrix, stored = files
    write_jsonl(stored, [])
    calls = []

    async def fake_search(query, engines, *, num_results=5, goal=None, published_after=None, session=None):
        calls.append((query, tuple(engines), num_results, goal))
        return {e: EngineRun(engine=e, status="ok", results=[Result(**r) for r in results("qx", e)])
                for e in engines}

    monkeypatch.setattr(telem_client, "search_engines", fake_search)
    m = await checker_eval.run("learn", engines=["tavily"], today=TODAY, matrix=matrix, results_path=stored,
                               questions=QUESTIONS, fill=True)
    assert m["n_searches"] == 3 and m["skipped_no_results"] == 0
    assert {c[0] for c in calls} == {"question q1", "question q2", "question q3"}
    assert all(c[1] == tuple(checker_eval.ENGINES) and c[2] == 5 and c[3] == c[0] for c in calls)


async def test_missing_matrix_is_a_clear_error(tmp_path, model):
    with pytest.raises(FileNotFoundError):
        await checker_eval.run("learn", engines=["tavily"], today=TODAY, matrix=tmp_path / "none.jsonl",
                               results_path=tmp_path / "none2.jsonl", questions=QUESTIONS)


def _scored(truth_hit: bool, verdict: str, others=(), category="fact") -> Scored:
    case = Case(q("x", category=category), "tavily", [Result(**results("x", "tavily")[0])], truth_hit,
                list(others))
    rank = 1 if verdict == "HIT" else None
    return Scored(case, CheckerVerdict(verdict=verdict, confidence=0.9, why="w", evidence_rank=rank))


def test_metrics_match_score_definitions():
    # Misses: one wrong HIT, two STALE and one DEAD that another engine rescues, two true DEADs.
    scored = ([_scored(False, "HIT", [True])] + [_scored(False, "STALE", [True])] * 2
              + [_scored(False, "DEAD", [False, True])] + [_scored(False, "DEAD", [False])] * 2
              + [_scored(True, "HIT")] * 3 + [_scored(True, "SHALLOW")])
    m = metrics(scored)
    assert m["miss_recall"] == pytest.approx(5 / 6, abs=1e-4)
    assert m["false_alarm_rate"] == 0.25
    assert m["hit_precision"] == 0.75
    assert checker_eval.report(m).endswith("target NOT MET")
    assert checker_eval.report(m, target_miss_recall=0.8, target_false_alarm=0.3).endswith("target MET")


def test_metrics_on_no_cases_do_not_divide_by_zero():
    m = metrics([])
    assert (m["miss_recall"], m["false_alarm_rate"], m["n_searches"]) == (0.0, 0.0, 0)


async def test_checker_outage_stops_the_eval(files, monkeypatch):
    async def down(system, user, schema=None):
        raise RuntimeError("401 invalid key")

    monkeypatch.setattr(checker, "_call_model", down)
    matrix, stored = files
    with pytest.raises(checker.CheckerError):
        await checker_eval.run("learn", engines=["tavily"], today=TODAY, matrix=matrix, results_path=stored,
                               questions=QUESTIONS)


async def test_limit_checks_only_the_first_cases(files, model):
    matrix, stored = files
    m = await checker_eval.run("learn", engines=["tavily"], today=TODAY, matrix=matrix, results_path=stored,
                               questions=QUESTIONS, limit=2)
    assert m["n_searches"] == 2 and len(model.prompts) == 2


async def test_benchmark_copies_are_dropped_like_the_matrix_judge_did(files, model):
    matrix, stored = files
    rows = [json.loads(line) for line in stored.read_text().splitlines()[:-1]]
    for row in rows:
        if (row["question_id"], row["engine"]) == ("q3", "tavily"):
            row["results"][0].update(url="https://huggingface.co/datasets/basicv8vc/SimpleQA",
                                     snippet="ANSWER_HERE")
    write_jsonl(stored, rows)
    m = await checker_eval.run("learn", engines=["tavily"], today=TODAY, matrix=matrix, results_path=stored,
                               questions=QUESTIONS)
    assert m["contaminated_results_dropped"] == 1
    assert m["confusion"]["miss"]["HIT"] == 0
    assert not any("huggingface" in p for p in model.prompts)
