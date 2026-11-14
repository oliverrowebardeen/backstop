"""Isolate A1 tests from deployment settings, generated tables, caches and real keys."""

import pytest

from so import config
from so.contracts import RescueTable


@pytest.fixture(autouse=True)
def a1_settings(monkeypatch, tmp_path):
    settings = {
        "MAX_RESCUES": 2,
        "SEARCH_MAX_COST_USD": 0.03,
        "SEARCH_TIMEOUT_S": 12.0,
        "CHECKER_TIMEOUT_S": 4.0,
        "TELEM_NUM_RESULTS": 5,
        "FRESHNESS_DAYS": 90,
        "RESCUE_TABLE_PATH": tmp_path / "rescue_table.json",
        "SO_LOG_DIR": tmp_path / "out" / "logs",
        "CACHE_DIR": tmp_path / "cache",
        "SO_CACHE": "readwrite",
        "FIRST_ENGINE": "tavily",
        "fallback_order": ["exa", "perplexity", "brave"],
        "SEARCH_BUDGET": config.Budget(5.0),
    }
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "api_key", lambda name: "test-key")
    monkeypatch.setattr("so.second_opinion.load_rescue_table", lambda: RescueTable())
