"""tests run on the fake Telem and LLM (tests/s2/fakes.py), injected through
evals.deps, with every eval output and the judge cache under tmp_path."""
import pytest


@pytest.fixture(autouse=True)
def eval_fakes(monkeypatch, tmp_path):
    from evals import deps
    from tests.s2 import fakes

    monkeypatch.setattr(deps, "OUT", tmp_path / "out")
    monkeypatch.setattr(deps, "JUDGE_CACHE", tmp_path / "cache" / "judge")
    monkeypatch.setattr(deps, "llm", fakes)
    monkeypatch.setattr(deps, "telem_client", fakes)
    return deps
