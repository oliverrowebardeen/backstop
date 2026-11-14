"""Single import point for the evals package (lanes S2, S4).

Real runs use Oliver's so.telem_client and so.llm. Test doubles from
tests/s2/fakes.py are used ONLY when SO_FAKE=1, and in that mode every output
goes to out/fake/ and every judge cache entry to a separate directory, so fake
data can never overwrite a real artifact.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from so import config
from so.contracts import EngineRun, MatrixRow, Question, Result  # noqa: F401

ROOT = config.ROOT
FAKE = os.getenv("SO_FAKE") == "1"
OUT: Path = ROOT / "out" / "fake" if FAKE else ROOT / "out"
JUDGE_CACHE: Path = config.CACHE_DIR / ("judge_fake" if FAKE else "judge")
ENGINES: list[str] = list(config.ENGINES)

if FAKE:
    print("[evals] SO_FAKE=1: fake Telem, fake LLM, outputs in out/fake/", file=sys.stderr)
    from tests.s2 import fakes as llm  # noqa: F401
    from tests.s2 import fakes as telem_client  # noqa: F401
else:
    from so import llm, telem_client  # noqa: F401


def get(obj, name, default=None):
    """Read a field from a pydantic model, dataclass or dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
