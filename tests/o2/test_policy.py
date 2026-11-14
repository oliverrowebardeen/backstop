from datetime import date
import pytest
from pydantic import ValidationError

from so import config
from so.contracts import CheckerVerdict, RescueTable, Result
from so.memory import RunMemory
from so.policy import freshness_cutoff, load_rescue_table, next_action


def verdict(label: str, **kwargs) -> CheckerVerdict:
    return CheckerVerdict(verdict=label, confidence=0.9, why="test evidence", **kwargs)


@pytest.mark.parametrize("rescues", [0, 1, 2])
def test_hit_stops_even_on_last_rescue(rescues):
    assert next_action(verdict("HIT"), ["tavily"], rescues, RescueTable()).kind == "HIT"


@pytest.mark.parametrize("label", ["STALE", "WRONG_ENTITY", "SHALLOW", "DEAD"])
def test_two_rescue_cap(label):
    action = next_action(verdict(label, best_url="https://example.com"), ["tavily"], 2, RescueTable())
    assert action.kind == "NOT_FOUND"


@pytest.mark.parametrize("label", ["STALE", "WRONG_ENTITY", "DEAD"])
def test_retry_uses_rewrite_and_learned_order(label):
    table = RescueTable(order_after={"tavily": ["tavily", "brave", "exa"]})
    action = next_action(verdict(label, rewrite="specific query"), ["tavily"], 0, table)
    assert (action.kind, action.engine, action.query) == ("RETRY_ENGINE", "brave", "specific query")
    assert action.fresh == (label == "STALE")


def test_shallow_reads_best_page():
    action = next_action(verdict("SHALLOW", best_url="https://example.com"), ["tavily"], 0, RescueTable())
    assert (action.kind, action.url) == ("FETCH_PAGE", "https://example.com")


def test_shallow_without_url_retries():
    assert next_action(verdict("SHALLOW"), ["tavily"], 0, RescueTable()).kind == "RETRY_ENGINE"


def test_engine_selection_uses_last_miss_and_skips_every_tried_engine():
    table = RescueTable(order_after={"exa": ["tavily", "exa", "brave"]})
    assert next_action(verdict("DEAD"), ["tavily", "exa"], 1, table).engine == "brave"


def test_exhausted_engines_stop():
    table = RescueTable(fallback_order=["tavily", "exa"])
    assert next_action(verdict("DEAD"), ["tavily", "exa"], 1, table).kind == "NOT_FOUND"


def test_missing_table_uses_config_and_picks_up_new_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "fallback_order", ["brave", "exa"])
    path = tmp_path / "rescue_table.json"
    assert load_rescue_table(path).fallback_order == ["brave", "exa"]
    path.write_text(RescueTable(order_after={"tavily": ["ceramic"]}).model_dump_json())
    assert load_rescue_table(path).order_after == {"tavily": ["ceramic"]}


def test_test_split_table_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "rescue_table.json"
    path.write_text('{"built_on_split": "test"}')
    with pytest.raises(ValidationError):
        load_rescue_table(path)


@pytest.mark.parametrize(("goal", "expected"), [
    ("find the current valuation", "2026-08-16"),
    ("find the 2023 round", "2023-01-01"),
    ("since 2026-10-02", "2026-10-02"),
    ("since 2026/10/02", "2026-10-02"),
    ("from 2026-10", "2026-10-01"),
    ("since March 14, 2023", "2023-03-14"),
    ("since 14 March 2023", "2023-03-14"),
    ("in Mar. 2023", "2023-03-01"),
    ("between 2023-03-14 and 2023-04-01", "2023-03-14"),
    ("invalid 2026-02-30", "2026-08-16"),
])
def test_freshness_cutoff(goal, expected):
    assert freshness_cutoff(goal, date(2026, 11, 14)) == expected


def test_memory_is_per_run_and_preserves_rewrites():
    memory = RunMemory()
    result = Result(rank=1, title="Source", url="https://example.com", engine="tavily")
    memory.record_search("query", "tavily", [result])
    memory.record_search("query", "exa", [result])
    memory.record_search("rewrite", "brave", [])
    memory.record_fetch("https://example.com/page")
    assert memory.queries == ["query", "query", "rewrite"]
    assert memory.engines_by_query == {"query": {"tavily", "exa"}, "rewrite": {"brave"}}
    assert memory.urls_seen == {"https://example.com", "https://example.com/page"}
    assert RunMemory().queries == []
    assert RunMemory().urls_seen == set()


def test_config_can_lower_the_rescue_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_RESCUES", 1)
    assert next_action(verdict("DEAD"), ["tavily"], 1, RescueTable()).kind == "NOT_FOUND"


def test_config_controls_freshness_window(monkeypatch):
    monkeypatch.setattr(config, "FRESHNESS_DAYS", 14)
    assert freshness_cutoff("current valuation", date(2026, 11, 14)) == "2026-10-31"
