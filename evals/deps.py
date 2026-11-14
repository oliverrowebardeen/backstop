"""Single import point for the evals package (lanes S2, S4).

Dependencies are plain module attributes, read at call time by the rest of the
package: telem_client and llm (Oliver's so.telem_client and so.llm), OUT (where
every artifact goes) and JUDGE_CACHE (the judge's disk cache). Callers that need
other implementations or locations replace these attributes before running.
"""
from __future__ import annotations

from pathlib import Path

from so import config, llm, telem_client  # noqa: F401
from so.contracts import EngineRun, MatrixRow, Question, Result  # noqa: F401

ROOT = config.ROOT
OUT: Path = ROOT / "out"
JUDGE_CACHE: Path = config.CACHE_DIR / "judge"
ENGINES: list[str] = list(config.ENGINES)


def get(obj, name, default=None):
    """Read a field from a pydantic model, dataclass or dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
