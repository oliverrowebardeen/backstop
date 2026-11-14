"""GET /results keeps provenance that the frozen contracts do not name."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from server.app import ServerSettings, create_app
from so.contracts import Frontier, Results

ROOT = Path(__file__).resolve().parents[2]

FRONTIER = {
    "split": "test", "non_trap_only": True, "generated_from": "real data",
    "points": [
        {"kind": "combo", "engines": ["tavily"], "coverage": 0.685, "cost_per_q": 0.005, "n_questions": 200},
        {"kind": "telem_default", "engines": [], "coverage": 0.9, "cost_per_q": 0.02, "n_questions": 200},
        {"kind": "second_opinion", "engines": [], "coverage": 0.86, "cost_per_q": 0.0083, "n_questions": 200,
         "simulated": True, "label": "simulated, perfect checker",
         "note": "simulated from the matrix: tavily first, up to 2 rescues in learned order."},
    ],
}

RESULTS = {
    "generated_at": "2026-11-14T22:00:00Z", "n_test": 200, "n_traps": 30,
    "setups": {"single": {"correct": 120, "wrong": 50, "declined": 30, "n": 200, "notes": ["cache on"]}},
    "checker": {"n_searches": 400, "miss_recall": 0.87, "false_alarm_rate": 0.1, "rescuable_vs_dead_acc": 0.7,
                "n_labeled": 380},
    "judge_agreement": {"n_checked": 30, "agree": 29, "labeler": "claude-sonnet-5-5 (blind)"},
    "warnings": ["2 finished logs had no answer event"],
    "contamination": {"single": {"seen": 1000, "removed": 40}},
}


def client_for(tmp_path: Path) -> TestClient:
    out = tmp_path / "out"
    out.mkdir()
    (out / "frontier.json").write_text(json.dumps(FRONTIER))
    (out / "results.json").write_text(json.dumps(RESULTS))
    settings = ServerSettings(
        ui_dir=ROOT / "ui", demo_dir=tmp_path / "demo", fixture_dir=None, log_dir=out / "logs",
        results_path=out / "results.json", frontier_path=out / "frontier.json", max_active_runs=8,
        max_question_chars=2000, start_timeout=1, run_timeout=2, poll_seconds=0.005, keepalive_seconds=0.1,
    )
    return TestClient(create_app(settings=settings))


def test_simulated_frontier_point_keeps_its_provenance(tmp_path):
    body = client_for(tmp_path).get("/results")
    assert body.status_code == 200
    frontier = body.json()["frontier"]
    simulated = frontier["points"][2]
    assert simulated["simulated"] is True and simulated["label"] == "simulated, perfect checker"
    assert simulated["n_questions"] == 200 and simulated["note"].startswith("simulated from the matrix")
    assert frontier["points"][0] == FRONTIER["points"][0]
    assert frontier["generated_from"] == "real data" and frontier["non_trap_only"] is True


def test_results_keep_counts_warnings_and_labeler(tmp_path):
    results = client_for(tmp_path).get("/results").json()["results"]
    assert results["setups"]["single"]["n"] == 200 and results["setups"]["single"]["notes"] == ["cache on"]
    assert results["warnings"] == ["2 finished logs had no answer event"]
    assert results["judge_agreement"]["labeler"] == "claude-sonnet-5-5 (blind)"
    assert results["contamination"]["single"]["removed"] == 40
    assert results["checker"]["n_labeled"] == 380
    # Frozen fields still validate and fill their defaults.
    assert results["setups"]["single"]["accuracy"] == 0 and results["setups"]["single"]["correct"] == 120


def test_contracts_still_require_the_frozen_fields():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Results.model_validate({"n_test": 1, "n_traps": 0})  # generated_at missing
    with pytest.raises(ValidationError):
        Frontier.model_validate({"points": [{"kind": "combo", "coverage": 2, "cost_per_q": 0}]})
