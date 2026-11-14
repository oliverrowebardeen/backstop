"""Evidence delivery defaults and overrides are validated at process startup."""

import json
import os
import subprocess
import sys

import pytest


def settings(**overrides: str) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("EVIDENCE_")}
    environment.update(SO_ENV_FILE=os.devnull, **overrides)
    return subprocess.run([
        sys.executable, "-c", "import json; from so import config; "
        "print(json.dumps([config.EVIDENCE_FILTER_ENABLED, config.EVIDENCE_EXTRA_RESULTS]))",
    ], env=environment, capture_output=True, text=True, check=False)


def test_evidence_defaults_retain_one_extra_result():
    result = settings()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, 1]


@pytest.mark.parametrize("extras", ["0", "3"])
def test_extra_result_count_is_configurable(extras):
    result = settings(EVIDENCE_FILTER_ENABLED="false", EVIDENCE_EXTRA_RESULTS=extras)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [False, int(extras)]


@pytest.mark.parametrize("value", ["-1", "1.5", "nan"])
def test_invalid_extra_counts_fail_clearly(value):
    result = settings(EVIDENCE_EXTRA_RESULTS=value)
    assert result.returncode != 0
    assert "EVIDENCE_EXTRA_RESULTS" in result.stderr


def test_invalid_filter_switch_fails_clearly():
    result = settings(EVIDENCE_FILTER_ENABLED="sometimes")
    assert result.returncode != 0
    assert "EVIDENCE_FILTER_ENABLED" in result.stderr
