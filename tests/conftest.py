"""Test-wide guard: the test suite never spends money on live APIs.

pytest imports this file before any test module, so it runs before so.config
reads .env. Blanking the provider keys here (so.config.load_env never overrides a
variable that is already set) makes any accidental live call raise
"<PROVIDER>_API_KEY is required" immediately instead of billing Telem, Tavily or
a model provider. A test that silently fell through to a real client, for
example because a fixture forgot to inject a fake, now fails loudly. Set BACKSTOP_LIVE_TESTS=1 to run deliberate live checks.
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
    config.addinivalue_line(
        "markers", "finance_deep_read: opt into configured finance page reads",
    )
    config.addinivalue_line(
        "markers", "evidence_delivery: opt into compact HIT result delivery",
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


@pytest.fixture(autouse=True)
def isolate_finance_deep_read(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy rescue fixtures opt out; finance tests exercise the enabled path."""
    if (request.node.get_closest_marker("finance_deep_read") is not None
            or (request.node.path.parent == Path(__file__).parent / "o2"
                and request.node.path.name.startswith("test_finance"))):
        return
    from so import config, second_opinion

    monkeypatch.setattr(config, "FINANCE_DEEP_READ", False)
    if second_opinion.config is not config:
        monkeypatch.setattr(second_opinion.config, "FINANCE_DEEP_READ", False)


@pytest.fixture(autouse=True)
def isolate_evidence_delivery(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy all-results fixtures explicit; delivery tests opt in."""
    if (request.node.get_closest_marker("evidence_delivery") is not None
            or (request.node.path.parent == Path(__file__).parent / "o2"
                and request.node.path.name.startswith("test_evidence"))):
        return
    from so import config, second_opinion

    monkeypatch.setattr(config, "EVIDENCE_FILTER_ENABLED", False)
    if second_opinion.config is not config:
        monkeypatch.setattr(second_opinion.config, "EVIDENCE_FILTER_ENABLED", False)
