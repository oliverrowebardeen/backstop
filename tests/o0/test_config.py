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
