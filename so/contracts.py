"""Shared wire contracts for retrieval, evaluation and run events.

RunContext.log and .memory hold runtime objects and are not serialized.
LLM tools use {id, name, arguments}; messages use OpenAI-style dictionaries.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from so import config

Setup = Literal["single", "telem_default", "second_opinion"]
Verdict = Literal["HIT", "STALE", "WRONG_ENTITY", "SHALLOW", "DEAD"]
EventType = Literal["run_start", "search", "check", "rescue", "fetch", "search_done", "llm", "answer", "run_end"]


class Question(BaseModel):
    id: str
    question: str
    gold: list[str]
    category: Literal["fact", "finance", "trap"]
    source: str
    split: Literal["learn", "test"]
    is_trap: bool = False
    trap_note: str | None = None


class Result(BaseModel):
    rank: int = Field(ge=1)
    title: str
    url: str
    snippet: str = ""
    published: str | None = None
    engine: str


class EngineRun(BaseModel):
    engine: str
    status: Literal["ok", "error"]
    results: list[Result] = Field(default_factory=list)
    latency_ms: float = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    cached: bool = False
    session_id: str | None = None


class PageText(BaseModel):
    url: str
    ok: bool
    text: str = ""
    latency_ms: float = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    cached: bool = False
    session_id: str | None = None


class MatrixRow(BaseModel):
    question_id: str
    engine: str
    hit: bool
    hit_rank: int | None = None
    judge_reason: str = ""
    n_results: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    engines_used: list[str] = Field(default_factory=list)


class RescueRate(BaseModel):
    rescued: int = Field(ge=0)
    misses: int = Field(ge=0)
    rate: float = Field(ge=0, le=1)


class RescueTable(BaseModel):
    built_on_split: Literal["learn"] = "learn"
    n_questions: int = Field(default=0, ge=0)
    miss_counts: dict[str, int] = Field(default_factory=dict)
    rescue: dict[str, dict[str, RescueRate]] = Field(default_factory=dict)
    order_after: dict[str, list[str]] = Field(default_factory=dict)
    fallback_order: list[str] = Field(default_factory=lambda: list(config.fallback_order))


class CheckerVerdict(BaseModel):
    verdict: Verdict
    confidence: float = Field(ge=0, le=1)
    why: str
    evidence_rank: int | None = Field(default=None, ge=1)
    best_url: str | None = None
    rewrite: str | None = None


class SearchOutcome(BaseModel):
    status: Literal["HIT", "NOT_FOUND", "UNCHECKED"]
    results: list[Result] = Field(default_factory=list)
    note: str = ""
    engines_called: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    calls: int = Field(default=0, ge=0)
    fetches: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    latency_ms: float = Field(default=0, ge=0)


class AgentAnswer(BaseModel):
    final: str
    status: Literal["ANSWERED", "NOT_FOUND"]
    citations: list[str] = Field(default_factory=list)
    searches: int = Field(default=0, ge=0)


class Event(BaseModel):
    ts: str
    run_id: str
    question_id: str
    setup: Setup
    seq: int = Field(ge=1)
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def fetch_has_via(self) -> Event:
        if self.type == "fetch" and self.data.get("via") not in {"tavily", "telem"}:
            raise ValueError("fetch events require via=tavily or via=telem")
        return self


class SetupResults(BaseModel):
    correct: int = 0
    wrong: int = 0
    declined: int = 0
    accuracy: float = 0
    wrong_rate: float = 0
    engine_calls_per_q: float = 0
    cost_per_q_search: float = 0
    cost_per_q_total: float = 0
    cost_per_correct: float | None = None
    cost_per_search_call: float | None = None
    latency_p50_ms: float = 0
    latency_p95_ms: float = 0
    trap_n: int = 0
    trap_confident_wrong: int = 0


class CheckerMetrics(BaseModel):
    n_searches: int = 0
    miss_recall: float = 0
    false_alarm_rate: float = 0
    rescuable_vs_dead_acc: float = 0


class JudgeAgreement(BaseModel):
    n_checked: int = 0
    agree: int = 0


class Results(BaseModel):
    generated_at: str
    n_test: int
    n_traps: int
    setups: dict[Setup, SetupResults] = Field(default_factory=dict)
    checker: CheckerMetrics = Field(default_factory=CheckerMetrics)
    judge_agreement: JudgeAgreement = Field(default_factory=JudgeAgreement)


class FrontierPoint(BaseModel):
    kind: Literal["combo", "telem_default", "second_opinion"]
    engines: list[str] = Field(default_factory=list)
    coverage: float = Field(ge=0, le=1)
    cost_per_q: float = Field(ge=0)


class Frontier(BaseModel):
    split: Literal["test"] = "test"
    points: list[FrontierPoint] = Field(default_factory=list)


class RunContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)
    run_id: str
    question_id: str
    setup: Setup
    session: str | None = Field(default=None, validation_alias=AliasChoices("session", "session_id", "telem_session_id"))
    log: Any = Field(default=None, exclude=True, validation_alias=AliasChoices("log", "eventlog"))
    memory: Any = Field(default=None, exclude=True)
    today: date = Field(default_factory=date.today)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def telem_session_id(self) -> str | None:
        return self.session

    @telem_session_id.setter
    def telem_session_id(self, value: str | None) -> None:
        self.session = value


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    latency_ms: float = Field(default=0, ge=0)
    model: str = ""
    role: Literal["agent", "checker", "judge"] = "agent"
    cached: bool = False
    stop_reason: str | None = None
