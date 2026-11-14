import asyncio

import pytest

from so import config


def test_dotenv_preserves_exported_values_and_quotes(tmp_path, monkeypatch):
    monkeypatch.setenv("O0_EXISTING", "shell")
    monkeypatch.delenv("O0_QUOTED", raising=False)
    path = tmp_path / ".env"
    path.write_text('O0_EXISTING=file\nexport O0_QUOTED="a # b" # comment\n')
    config.load_env(path)
    assert config.os.environ["O0_EXISTING"] == "shell"
    assert config.os.environ["O0_QUOTED"] == "a # b"


def test_budget_reservations_prevent_concurrent_overspend():
    budget = config.Budget(0.01)
    budget.reserve(0.006)
    with pytest.raises(config.BudgetExceeded):
        budget.reserve(0.005)
    budget.settle(0.006, 0.004)
    budget.reserve(0.006)
    budget.settle(0.006, 0.006)
    with pytest.raises(config.BudgetExceeded):
        budget.reserve(0.001)
    assert budget.spent == pytest.approx(0.01)


async def test_retry_only_retries_transient_status(monkeypatch):
    class Error(Exception):
        status_code = 429
    attempts = 0
    delays = []

    async def sleep(delay):
        delays.append(delay)

    async def call():
        nonlocal attempts
        attempts += 1
        raise Error()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(Error):
        await config.retry(call)
    assert attempts == 4
    assert delays == [0.5, 1, 2]
    Error.status_code = 401
    attempts = 0
    with pytest.raises(Error):
        await config.retry(call)
    assert attempts == 1


def test_environment_overrides_are_loaded_in_fresh_process(tmp_path):
    import json
    import os
    import subprocess
    import sys
    env = dict(os.environ, SO_ENV_FILE=str(tmp_path / "absent.env"), ENGINES="one,two",
               FIRST_ENGINE="two", SUBSTITUTES="three", FALLBACK_ORDER="three,one",
               PRICE_USD_JSON='{"one":0.02}', DEFAULT_PRICE_USD="0.03", TELEM_FETCH_PRICE_USD="0.04",
               SO_OUT_DIR=str(tmp_path / "artifacts"), SO_LOG_DIR=str(tmp_path / "logs"))
    code = "from so import config as c; import json; print(json.dumps([c.ENGINES,c.FIRST_ENGINE,c.SUBSTITUTES,c.fallback_order,c.PRICE_USD['one'],c.DEFAULT_PRICE_USD,c.TELEM_FETCH_PRICE_USD,str(c.OUT_DIR),str(c.SO_LOG_DIR)]))"
    result = subprocess.run([sys.executable, "-c", code], env=env, check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == [["one", "two"], "two", ["three"], ["three", "one"], .02, .03, .04,
                                       str(tmp_path / "artifacts"), str(tmp_path / "logs")]


def test_rescue_table_defaults_follow_configuration(monkeypatch):
    from so.contracts import RescueTable
    monkeypatch.setattr(config, "fallback_order", ["custom"])
    table = RescueTable()
    assert table.fallback_order == ["custom"]
    table.fallback_order.append("another")
    assert config.fallback_order == ["custom"]
