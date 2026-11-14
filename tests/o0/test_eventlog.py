from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from pydantic import ValidationError

from so.eventlog import EventLog, read_log, summarize


def test_summary_counts_checker_once_and_excludes_judge(tmp_path):
    log = EventLog("single-q-1", "q", "single", log_dir=tmp_path)
    log.emit("run_start", question="question")
    log.emit("search", engines=["tavily", "exa"], cost_usd=.01)
    log.emit("fetch", url="https://example.com", ok=True, via="tavily", cost_usd=.0016)
    log.emit("llm", role="checker", cost_usd=.002)
    log.emit("llm", role="agent", cost_usd=.003)
    log.emit("llm", role="judge", cost_usd=.1)
    log.emit("search_done", cost_usd=.0136)
    totals = summarize(log.path)
    assert totals["engine_calls"] == 2 and totals["fetches"] == 1 and totals["searches"] == 1
    assert totals["cost_usd_search"] == pytest.approx(.0116)
    assert totals["cost_usd_llm"] == pytest.approx(.005)
    assert totals["cost_usd_total"] == pytest.approx(.0166)
    log.emit("run_end", **totals)
    assert summarize(log.path) == totals


def test_concurrent_events_and_resume_have_unique_sequences(tmp_path):
    log = EventLog("single-q-2", "q", "single", log_dir=tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: log.emit("check", verdict="HIT", confidence=1, why=str(i)), range(20)))
    resumed = EventLog("single-q-2", "q", "single", log_dir=tmp_path)
    resumed.emit("answer", final="done", status="ANSWERED", citations=[])
    rows = read_log(log.path)
    assert [row["seq"] for row in rows] == list(range(1, 22))
    assert all(row["ts"].endswith("Z") for row in rows)


def test_requires_fetch_via_and_rejects_path_escape(tmp_path):
    log = EventLog("single-q-3", "q", "single", log_dir=tmp_path)
    with pytest.raises(ValidationError, match="via"):
        log.emit("fetch", url="https://example.com", ok=True)
    with pytest.raises(ValueError):
        EventLog("../escape", "q", "single", log_dir=tmp_path)


def test_trailing_partial_line_is_ignored_but_corruption_is_rejected(tmp_path):
    log = EventLog("single-q-4", "q", "single", log_dir=tmp_path)
    log.emit("run_start", question="q")
    with log.path.open("a") as file:
        file.write('{"ts":')
    assert len(read_log(log.path)) == 1
    with log.path.open("a") as file:
        file.write('\n')
    with pytest.raises(json.JSONDecodeError):
        read_log(log.path)


def test_default_log_directory_comes_from_config(tmp_path, monkeypatch):
    from so import config
    monkeypatch.setattr(config, "SO_LOG_DIR", tmp_path)
    log = EventLog("single-q-config", "q", "single")
    assert log.path.parent == tmp_path
