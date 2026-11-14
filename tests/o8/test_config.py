import json
import os
import subprocess
import sys

import pytest


def read_settings(tmp_path, **overrides):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("FIRST_ENGINE_")}
    environment.update(SO_ENV_FILE=str(tmp_path / "absent.env"), SO_OUT_DIR=str(tmp_path), **overrides)
    return subprocess.run(
        [sys.executable, "-c", "from so import config; import json; "
         "print(json.dumps({k: str(v) for k, v in vars(config).items() if k.startswith('FIRST_ENGINE_')}))"],
        env=environment, text=True, capture_output=True, check=False,
    )


def test_default_paths_follow_shared_output_directory(tmp_path):
    result = read_settings(tmp_path)
    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout)
    assert settings["FIRST_ENGINE_ROUTING"] == "on"
    assert settings["FIRST_ENGINE_TABLE_PATH"] == str(tmp_path / "first_engine.json")
    assert settings["FIRST_ENGINE_MATRIX_PATH"] == str(tmp_path / "matrix.jsonl")
    assert settings["FIRST_ENGINE_QUESTIONS_PATH"].endswith("data/questions.jsonl")
    assert settings["FIRST_ENGINE_TRAPS_PATH"].endswith("data/traps.jsonl")


def test_environment_overrides(tmp_path):
    overrides = {
        "FIRST_ENGINE_ROUTING": "off", "FIRST_ENGINE_TABLE_PATH": "custom/routing.json",
        "FIRST_ENGINE_MATRIX_PATH": "custom/matrix.jsonl", "FIRST_ENGINE_QUESTIONS_PATH": "custom/questions.jsonl",
        "FIRST_ENGINE_TRAPS_PATH": "custom/traps.jsonl", "FIRST_ENGINE_FINANCE_PATTERN": r"\bfinancials\b",
    }
    result = read_settings(tmp_path, **overrides)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == overrides


@pytest.mark.parametrize("overrides", [
    {"FIRST_ENGINE_ROUTING": "sometimes"}, {"FIRST_ENGINE_FINANCE_PATTERN": "["},
    {"FIRST_ENGINE_FINANCE_PATTERN": ""},
])
def test_invalid_settings_fail_clearly(tmp_path, overrides):
    result = read_settings(tmp_path, **overrides)
    assert result.returncode != 0
    assert next(iter(overrides)) in result.stderr
