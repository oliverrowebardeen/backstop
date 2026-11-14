"""Keep routing tests independent of deployed tables and provider credentials."""

import pytest

from so import config


@pytest.fixture(autouse=True)
def routing_settings(monkeypatch, tmp_path):
    settings = {
        "FIRST_ENGINE_ROUTING": "on",
        "FIRST_ENGINE_TABLE_PATH": tmp_path / "first_engine.json",
        "FIRST_ENGINE_QUESTIONS_PATH": tmp_path / "questions.jsonl",
        "FIRST_ENGINE_TRAPS_PATH": tmp_path / "traps.jsonl",
        "FIRST_ENGINE_MATRIX_PATH": tmp_path / "matrix.jsonl",
        "RESCUE_TABLE_PATH": tmp_path / "rescue_table.json",
        "ENGINES": ["tavily", "exa", "perplexity"],
        "FIRST_ENGINE": "tavily",
        "PRICE_USD": {"tavily": 0.005, "exa": 0.005, "perplexity": 0.005},
        "DEFAULT_PRICE_USD": 0.005,
        "fallback_order": ["exa", "perplexity", "tavily"],
        "MAX_RESCUES": 2,
        "SEARCH_TIMEOUT_S": 12.0,
        "CHECKER_TIMEOUT_S": 4.0,
        "SEARCH_MAX_COST_USD": 0.03,
    }
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)


@pytest.fixture
def learn_data():
    from so.contracts import MatrixRow, Question

    questions = [
        Question(id="fact-a", question="Who built the observatory?", category="fact", gold=[], source="unit", split="learn"),
        Question(id="fact-b", question="When did the city become a capital?", category="fact", gold=[], source="unit", split="learn"),
        Question(id="finance-a", question="What was the fiscal year revenue?", category="finance", gold=[], source="unit", split="learn"),
    ]
    hits = {"fact-a": {"perplexity"}, "fact-b": {"exa", "perplexity"}, "finance-a": {"exa"}}
    rows = [MatrixRow(question_id=q.id, engine=engine, hit=engine in hits[q.id])
            for q in questions for engine in config.ENGINES]
    return questions, rows
