"""Isolate tests from deployment settings, generated tables and real keys."""

import pytest

from so import config
from so.contracts import RescueTable


@pytest.fixture(autouse=True)
def s6_settings(monkeypatch, tmp_path):
    settings = {
        "MAX_RESCUES": 2,
        "SEARCH_MAX_COST_USD": 0.03,
        "SEARCH_TIMEOUT_S": 12.0,
        "CHECKER_TIMEOUT_S": 4.0,
        "TELEM_NUM_RESULTS": 5,
        "FRESHNESS_DAYS": 90,
        "PAGE_TEXT_CHARS": 6000,
        "RESCUE_TABLE_PATH": tmp_path / "rescue_table.json",
        "SO_LOG_DIR": tmp_path / "out" / "logs",
        "FIRST_ENGINE": "tavily",
        "fallback_order": ["exa", "perplexity", "brave", "parallel", "ceramic"],
    }
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr("so.second_opinion.load_rescue_table", lambda: RescueTable())
