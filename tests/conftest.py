"""Test-wide guard: the test suite never spends money on live APIs.

pytest imports this file before any test module, so it runs before so.config
reads .env. Blanking the provider keys here (so.config.load_env never overrides a
variable that is already set) makes any accidental live call raise
"<PROVIDER>_API_KEY is required" immediately instead of billing Telem, Tavily or
a model provider. A test that silently fell through to a real client, for
example because another module imported evals before SO_FAKE was set, now fails
loudly. Set BACKSTOP_LIVE_TESTS=1 to run deliberate live checks.
"""
import os
from pathlib import Path

import pytest

if os.getenv("BACKSTOP_LIVE_TESTS") != "1":
    for name in ("TELEM_API_KEY", "TAVILY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        os.environ[name] = ""


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "first_engine_routing: opt into configured first-engine routing",
    )


@pytest.fixture(autouse=True)
def isolate_first_engine_routing(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fixed-engine fixtures stable; routing tests opt in by marker or lane."""
    if (request.node.get_closest_marker("first_engine_routing") is not None
            or request.node.path.is_relative_to(Path(__file__).parent / "o8")):
        return
    from so import config, router

    monkeypatch.setattr(config, "FIRST_ENGINE_ROUTING", "off")
    # Matrix tests reload so.config; the router can retain the prior instance.
    if router.config is not config:
        monkeypatch.setattr(router.config, "FIRST_ENGINE_ROUTING", "off")
