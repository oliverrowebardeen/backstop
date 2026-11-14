"""Resolve Oliver's modules, or explicit fakes for tests (lanes S2, S4).

Contracts fall back to a schema-identical stand-in if O0 has not merged.
Paid modules (telem_client, llm, agent) fall back ONLY when SO_FAKE=1, and in
that mode every output goes to out/fake/ so fake data can never overwrite a
real artifact.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = os.getenv("SO_FAKE") == "1"
OUT = ROOT / "out" / "fake" if FAKE else ROOT / "out"

try:
    from so import contracts  # noqa: F401
    from so.contracts import EngineRun, MatrixRow, Question, Result  # noqa: F401
except ImportError:
    from tests.s2 import fake_contracts as contracts  # noqa: F401
    from tests.s2.fake_contracts import EngineRun, MatrixRow, Question, Result  # noqa: F401

if FAKE:
    print("[evals] SO_FAKE=1: fake Telem, fake LLM, outputs in out/fake/", file=sys.stderr)
    from tests.s2 import fakes as telem_client  # noqa: F401
    from tests.s2 import fakes as llm  # noqa: F401
else:
    try:
        from so import llm, telem_client  # noqa: F401
    except ImportError:
        if os.getenv("SO_INTERIM") != "1":
            raise
        # O0's wrappers not merged yet: same signatures, Oliver's config helpers.
        print("[evals] SO_INTERIM=1: so.telem_client/so.llm missing, using evals/interim_so.py",
              file=sys.stderr)
        from evals import interim_so as llm  # noqa: F401
        from evals import interim_so as telem_client  # noqa: F401

try:
    from so.config import ENGINES  # noqa: F401
except ImportError:
    ENGINES = ["tavily", "exa", "brave", "perplexity", "parallel", "ceramic"]


def get(obj, name, default=None):
    """Read a field from a pydantic model, dataclass or dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
