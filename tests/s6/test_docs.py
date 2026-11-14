"""Part E: packaging and user-facing docs stay consistent."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ["README.md", "CHANGELOG.md", "LICENSE", "docs/api.md"]


@pytest.mark.parametrize("name", DOCS)
def test_docs_exist_without_em_dashes(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert "—" not in text and "–" not in text


def test_readme_documents_the_one_line_swap_and_measurement_rules():
    text = " ".join((ROOT / "README.md").read_text(encoding="utf-8").replace("**", "").split())
    assert "BackstopTelem()" in text and "AsyncTelem()" in text
    assert "learn split only" in text and "held-out test split" in text
    assert "evals/contamination.py" in text and "## Results" in text


def test_changelog_version_matches_package():
    import backstop

    assert f"## [{backstop.__version__}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
