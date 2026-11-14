"""S4 tests run on test doubles only: no keys, no paid calls, nothing written to out/.

Every test gets the fake Telem and LLM (tests/s2/fakes.py) and the fake agent
(tests/s4/fake_agent.py) injected through the evals module attributes, with all
eval outputs and the judge cache under tmp_path.
"""
import pytest


@pytest.fixture(autouse=True)
def eval_fakes(monkeypatch, tmp_path):
    from evals import deps, run_eval
    from tests.s2 import fakes
    from tests.s4 import fake_agent

    monkeypatch.setattr(deps, "OUT", tmp_path / "out")
    monkeypatch.setattr(deps, "JUDGE_CACHE", tmp_path / "cache" / "judge")
    monkeypatch.setattr(deps, "llm", fakes)
    monkeypatch.setattr(deps, "telem_client", fakes)
    monkeypatch.setattr(run_eval, "AGENT", fake_agent)
    return deps
