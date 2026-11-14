"""Check server environment settings in fresh interpreters without shared secrets."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def read_config(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("SERVER_")}
    environment.update(SO_ENV_FILE=str(tmp_path / "absent.env"),
                       SO_OUT_DIR=str(tmp_path / "output"),
                       SO_LOG_DIR=str(tmp_path / "logs"), **overrides)
    code = (
        "from so import config; import json; "
        "print(json.dumps({key: str(value) if hasattr(value, '__fspath__') else value "
        "for key, value in vars(config).items() if key.startswith('SERVER_')}))"
    )
    return subprocess.run([sys.executable, "-c", code], env=environment,
                          text=True, capture_output=True, check=False)


def test_server_defaults_inherit_shared_paths_and_disable_fixtures(tmp_path: Path) -> None:
    result = read_config(tmp_path)
    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout)
    assert settings["SERVER_LOG_DIR"] == str(tmp_path / "logs")
    assert settings["SERVER_RESULTS_PATH"] == str(tmp_path / "output/results.json")
    assert settings["SERVER_FRONTIER_PATH"] == str(tmp_path / "output/frontier.json")
    assert settings["SERVER_FIXTURE_DIR"] is None
    assert settings["SERVER_LIVE_RACES"] is False
    assert settings["SERVER_HOST"] == "127.0.0.1"
    assert settings["SERVER_PORT"] == 8000


def test_server_settings_accept_environment_overrides(tmp_path: Path) -> None:
    overrides = {
        "SERVER_UI_DIR": "custom/ui", "SERVER_DEMO_DIR": "custom/demo",
        "SERVER_FIXTURE_DIR": "tests/o4/fixtures", "SERVER_LOG_DIR": "custom/logs",
        "SERVER_RESULTS_PATH": "custom/results.json", "SERVER_FRONTIER_PATH": "custom/frontier.json",
        "SERVER_MAX_ACTIVE_RUNS": "4", "SERVER_MAX_QUESTION_CHARS": "500",
        "SERVER_START_TIMEOUT_SECONDS": "20", "SERVER_RUN_TIMEOUT_SECONDS": "300",
        "SERVER_POLL_SECONDS": "0.2", "SERVER_KEEPALIVE_SECONDS": "15",
        "SERVER_HOST": "0.0.0.0", "SERVER_PORT": "8123", "SERVER_LIVE_RACES": "true",
    }
    result = read_config(tmp_path, **overrides)
    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout)
    for name, value in overrides.items():
        if isinstance(settings[name], bool):
            assert settings[name] is (value == "true")
        elif isinstance(settings[name], (int, float)):
            assert settings[name] == float(value)
        else:
            assert settings[name] == value


@pytest.mark.parametrize("overrides", [
    {"SERVER_MAX_ACTIVE_RUNS": "1"}, {"SERVER_MAX_QUESTION_CHARS": "0"},
    {"SERVER_START_TIMEOUT_SECONDS": "nan"}, {"SERVER_RUN_TIMEOUT_SECONDS": "inf"},
    {"SERVER_POLL_SECONDS": "0"}, {"SERVER_KEEPALIVE_SECONDS": "-1"},
    {"SERVER_LIVE_RACES": "invalid"}, {"SERVER_HOST": " "}, {"SERVER_PORT": "65536"},
])
def test_invalid_server_settings_fail_clearly(tmp_path: Path, overrides: dict[str, str]) -> None:
    result = read_config(tmp_path, **overrides)
    assert result.returncode != 0
    assert next(iter(overrides)) in result.stderr
