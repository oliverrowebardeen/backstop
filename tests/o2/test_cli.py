import json
import sys

import pytest

from so.contracts import RescueTable
from so.eventlog import summarize
from so.second_opinion import main
from tests.o2.fakes import Fakes, verdict


def test_cli_prints_outcome_and_writes_a_costed_log(tmp_path, monkeypatch, capsys):
    fake = Fakes([verdict("HIT", evidence_rank=1)])
    fake.install(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("so.second_opinion.load_rescue_table", lambda: RescueTable())
    monkeypatch.setattr(sys, "argv", ["so.second_opinion", "Stripe valuation", "--goal", "find the valuation"])
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "HIT" and output["calls"] == 1
    paths = list((tmp_path / "out" / "logs").glob("*.jsonl"))
    assert len(paths) == 1
    assert summarize(paths[0])["cost_usd_total"] == pytest.approx(output["cost_usd"])
