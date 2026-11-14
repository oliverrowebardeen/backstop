"""Sanitize run failures before logging them or sending them to a browser."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import traceback

import httpx
from pydantic import BaseModel

from so import config


class RunFailure(BaseModel):
    error_type: str
    message: str


def redact(text: str) -> str:
    """Remove configured credentials and common credential forms from diagnostics."""
    for name, value in os.environ.items():
        if value and re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)$", name, re.I):
            text = text.replace(value, "[redacted]")
    text = re.sub(r"\b(?:sk|tvly|telem)-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;'\"]+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)([\"']?(?:api[_-]?key|authorization|token|secret|password)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,&;}]+)", r"\1[redacted]", text)
    return text


def describe_failure(exc: BaseException) -> tuple[RunFailure, str]:
    """Keep provider error messages for diagnostics and give users a plain reason."""
    detail = str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.json()
            error = body.get("error", {}) if isinstance(body, dict) else {}
            message = error.get("message") if isinstance(error, dict) else error
            if isinstance(message, str):
                detail = f"{detail}: {message}"
        except ValueError:
            pass
    detail = redact(detail)
    lowered = detail.lower()
    if isinstance(exc, config.BudgetExceeded):
        message = "The configured run budget has been exhausted. Check the server budget before trying again."
    elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        message = "The run timed out before it could finish. Try again."
    elif any(term in lowered for term in ("usage limit", "credit balance", "quota", "spend limit", "billing limit")):
        message = "The provider's usage limit has been reached. Check its account limits or billing before trying again."
    elif isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            message = "The provider rejected authentication. Check the server credentials."
        elif status == 429:
            message = "The provider is limiting requests. Try again later."
        elif status >= 500:
            message = "The provider is unavailable. Try again later."
        else:
            message = "The provider rejected the request. Check the server configuration."
    else:
        message = "The run failed before an answer was completed. Check the server logs for the reason."
    return RunFailure(error_type=type(exc).__name__, message=message), detail


def log_failure(logger: logging.Logger, exc: BaseException) -> RunFailure:
    failure, detail = describe_failure(exc)
    # Format first so traceback messages and source lines receive the same redaction.
    diagnostic = redact("".join(traceback.format_exception(exc)))
    logger.exception("Race agent failed (%s): %s\n%s", failure.error_type, detail,
                     diagnostic, exc_info=False)
    return failure


def write_failure(directory: Path, failure: RunFailure) -> None:
    """Keep terminal error metadata beside the agent log for reconnects and restarts."""
    for path in directory.glob("*.jsonl"):
        target = path.with_suffix(".error.json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(failure.model_dump_json(), encoding="utf-8")
        temporary.replace(target)


def read_failure(path: Path) -> RunFailure | None:
    try:
        return RunFailure.model_validate_json(path.with_suffix(".error.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return RunFailure(error_type="UnreadableFailure", message="The run failed, but its error details could not be read.")
