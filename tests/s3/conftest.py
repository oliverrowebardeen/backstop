"""S3 tests never read or write the real out/: analysis outputs go under tmp_path."""
import pytest


@pytest.fixture(autouse=True)
def analysis_out(monkeypatch, tmp_path):
    from analysis import common, findings

    monkeypatch.setattr(common, "OUT", tmp_path / "out")
    monkeypatch.setattr(findings, "FINDINGS_MD", tmp_path / "findings.md")
    return common.OUT
