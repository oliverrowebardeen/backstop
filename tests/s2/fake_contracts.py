"""Stand-ins for so.contracts used by evals until O0 merges (section 6)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from tests.s1.fake_contracts import Question  # noqa: F401


class Result(BaseModel):
    rank: int
    title: str
    url: str
    snippet: str = ""
    published: str | None = None
    engine: str


class EngineRun(BaseModel):
    engine: str
    status: Literal["ok", "error"]
    results: list[Result] = []
    latency_ms: float = 0
    cost_usd: float = 0
    cached: bool = False


class MatrixRow(BaseModel):
    question_id: str
    engine: str
    hit: bool
    hit_rank: int | None = None
    judge_reason: str = ""
    n_results: int = 0
    latency_ms: float = 0
    cost_usd: float = 0
    engines_used: list[str] | None = None
