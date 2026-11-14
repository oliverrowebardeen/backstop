"""Diagnostics retain useful failure details while redacting credentials."""
import logging

import httpx
import pytest

from server.errors import describe_failure, log_failure


def test_exception_diagnostics_redact_credentials(monkeypatch, caplog):
    credential = "test-only-provider-credential"
    monkeypatch.setenv("ANTHROPIC_API_KEY", credential)
    request = httpx.Request("POST", "https://provider.invalid/messages")
    response = httpx.Response(400, request=request, json={"error": {
        "message": f"Usage limit reached for api_key={credential}"}})
    with caplog.at_level(logging.ERROR):
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            failure = log_failure(logging.getLogger("test.run_failure"), exc)
    assert "HTTPStatusError" in caplog.text and "Usage limit reached" in caplog.text
    assert credential not in caplog.text and credential not in failure.model_dump_json()
    assert "[redacted]" in caplog.text
    assert "usage limit" in failure.message


def test_nested_exception_tracebacks_are_redacted(monkeypatch, caplog):
    credential = "test-only-nested-credential"
    monkeypatch.setenv("OPENAI_API_KEY", credential)
    with caplog.at_level(logging.ERROR):
        try:
            try:
                raise ValueError(credential)
            except ValueError as cause:
                raise RuntimeError("Provider call failed") from cause
        except RuntimeError as exc:
            log_failure(logging.getLogger("test.run_failure"), exc)
    assert "RuntimeError" in caplog.text and "Provider call failed" in caplog.text
    assert credential not in caplog.text


@pytest.mark.parametrize("status, expected", [(401, "authentication"), (429, "limiting requests"), (500, "unavailable")])
def test_http_failures_have_plain_reasons(status, expected):
    request = httpx.Request("POST", "https://provider.invalid/messages")
    response = httpx.Response(status, request=request)
    failure, _ = describe_failure(httpx.HTTPStatusError("Provider request failed", request=request, response=response))
    assert expected in failure.message


def test_timeout_has_a_plain_reason():
    failure, _ = describe_failure(TimeoutError())
    assert "timed out" in failure.message
