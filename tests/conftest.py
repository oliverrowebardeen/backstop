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

if os.getenv("BACKSTOP_LIVE_TESTS") != "1":
    for name in ("TELEM_API_KEY", "TAVILY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        os.environ[name] = ""
