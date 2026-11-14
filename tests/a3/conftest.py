"""Isolate tests from deployment settings, generated rescue tables and the disk cache."""

import pytest

from so import config
from so.contracts import RescueTable


@pytest.fixture(autouse=True)
def a3_settings(monkeypatch, tmp_path):
    settings = {
        "MAX_RESCUES": 2,
        "TELEM_NUM_RESULTS": 5,
        "FRESHNESS_DAYS": 90,
        "PAGE_TEXT_CHARS": 6000,
        "RESCUE_TABLE_PATH": tmp_path / "rescue_table.json",
        "SO_LOG_DIR": tmp_path / "out" / "logs",
        "CACHE_DIR": tmp_path / "cache",
        "FIRST_ENGINE": "tavily",
        "fallback_order": ["exa", "perplexity", "brave", "parallel", "ceramic"],
    }
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr("so.second_opinion.load_rescue_table", lambda: RescueTable())
