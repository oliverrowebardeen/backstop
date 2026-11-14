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


def test_malformed_paid_response_is_cached_and_charged(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "SO_CACHE", "readwrite")
    budget = config.Budget(1)
    budget.reserve(.01)
    with pytest.raises(ValueError, match="Provider response failed validation"):
        with config.paid_response("bad", {"query": "q"}, {"invalid": True}, budget, .01):
            raise KeyError("usage")
    assert budget.spent == .01 and budget.reserved == 0
    assert json.loads((tmp_path / "bad.json").read_text())["raw"] == {"invalid": True}
    with pytest.raises(ValueError, match="Cached provider response failed validation"):
        config.read_cache("bad")


def test_cache_write_failure_does_not_erase_spend(monkeypatch):
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(config, "write_cache", fail)
    budget = config.Budget(1)
    budget.reserve(.01)
    with pytest.raises(OSError, match="disk full"):
        with config.paid_response("paid", {}, {}, budget, .01) as record:
            record.update(normalized={}, _cost_usd=.005)
    assert budget.spent == .005 and budget.reserved == 0


def test_agent_environment_settings_and_log_alias(tmp_path):
    import json
    import os
    import subprocess
    import sys
    env = dict(os.environ, SO_ENV_FILE=str(tmp_path / "absent.env"), AGENT_MAX_SEARCHES="2",
               AGENT_MAX_PAGE_READS="3", AGENT_MAX_TURNS="4", AGENT_PROMPT_PATH="custom-prompt.md",
               SO_LOG_DIR=str(tmp_path), SO_ENABLE_SECOND_OPINION="false")
    code = "from so import config as c; import json; print(json.dumps([c.AGENT_MAX_SEARCHES,c.AGENT_MAX_PAGE_READS,c.AGENT_MAX_TURNS,str(c.AGENT_PROMPT_PATH),c.SO_ENABLE_SECOND_OPINION,c.LOG_DIR == c.SO_LOG_DIR]))"
    result = subprocess.run([sys.executable, "-c", code], env=env, check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == [2, 3, 4, "custom-prompt.md", False, True]


@pytest.mark.parametrize("error", [
    pytest.param("rate_limit", id="tavily-usage-limit"),
    pytest.param("timeout", id="tavily-timeout"),
    pytest.param("server", id="httpx-server-error"),
])
async def test_retry_real_sdk_errors_with_configured_cap(monkeypatch, error):
    import httpx
    from tavily.errors import TimeoutError as TavilyTimeoutError, UsageLimitExceededError

    request = httpx.Request("POST", "https://example.com/extract")
    errors = {
        "rate_limit": UsageLimitExceededError("rate limited"),
        "timeout": TavilyTimeoutError(1),
        "server": httpx.HTTPStatusError("unavailable", request=request,
                                        response=httpx.Response(503, request=request)),
    }
    monkeypatch.setattr(config, "API_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "API_RETRY_BACKOFF_S", .1)
    attempts = 0
    delays = []

    async def sleep(delay):
        delays.append(delay)

    async def call():
        nonlocal attempts
        attempts += 1
        raise errors[error]

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(type(errors[error])) as raised:
        await config.retry(call)
    assert raised.value is errors[error]
    assert attempts == 3
    assert delays == [.1, .2]


async def test_httpx_timeout_propagates_to_caller_without_retry():
    import httpx

    attempts = 0

    async def call():
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timeout")

    with pytest.raises(httpx.ReadTimeout):
        await config.retry(call)
    assert attempts == 1


async def test_tavily_rate_limit_can_recover(monkeypatch):
    from tavily.errors import UsageLimitExceededError

    attempts = 0
    monkeypatch.setattr(config, "API_RETRY_BACKOFF_S", 0)

    async def call():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise UsageLimitExceededError("rate limited")
        return "recovered"

    assert await config.retry(call) == "recovered"
    assert attempts == 2


@pytest.mark.parametrize("error_type", ["BadRequestError", "ForbiddenError", "InvalidAPIKeyError"])
async def test_permanent_tavily_errors_are_not_retried(error_type):
    from tavily import errors

    attempts = 0
    error = getattr(errors, error_type)("permanent failure")

    async def call():
        nonlocal attempts
        attempts += 1
        raise error

    with pytest.raises(type(error)):
        await config.retry(call)
    assert attempts == 1
