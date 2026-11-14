"""Finance controls use validated environment values and deployable defaults."""

import json
import os
import subprocess
import sys

import pytest


def settings(**overrides: str) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("FINANCE_")}
    environment.update(SO_ENV_FILE=os.devnull, **overrides)
    return subprocess.run([
        sys.executable, "-c", "import json; from so import config; "
        "print(json.dumps([config.FINANCE_DEEP_READ, config.FINANCE_EXTRACT_CHUNKS]))",
    ], env=environment, capture_output=True, text=True, check=False)


def test_default_finance_read_is_enabled_with_five_chunks():
    result = settings()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, 5]


@pytest.mark.parametrize("chunks", ["1", "5"])
def test_finance_environment_overrides(chunks):
    result = settings(FINANCE_DEEP_READ="false", FINANCE_EXTRACT_CHUNKS=chunks)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [False, int(chunks)]


@pytest.mark.parametrize("value", ["0", "6", "1.5", "nan"])
def test_invalid_finance_chunk_counts_fail_clearly(value):
    result = settings(FINANCE_EXTRACT_CHUNKS=value)
    assert result.returncode != 0
    assert "FINANCE_EXTRACT_CHUNKS" in result.stderr


def test_invalid_finance_switch_fails_clearly():
    result = settings(FINANCE_DEEP_READ="sometimes")
    assert result.returncode != 0
    assert "FINANCE_DEEP_READ" in result.stderr
