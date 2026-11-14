"""Isolate policy tests from deployment settings and generated rescue tables."""

import pytest

from so import config


@pytest.fixture(autouse=True)
def policy_settings(monkeypatch, tmp_path):
    settings = {
        "MAX_RESCUES": 2,
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
