"""The diligence fields a fact sheet tracks, and how each one is searched and watched."""
from __future__ import annotations

from dataclasses import dataclass

#: Telem Monitors frequencies, by the names this package accepts.
FREQUENCIES: dict[str, str] = {"hourly": "1h", "daily": "1d", "weekly": "7d"}


@dataclass(frozen=True)
class FieldSpec:
    """One fact sheet field.

    The templates take ``{company}``. ``monitor`` is the one-sentence description of the
    change that would invalidate the field; it becomes the Telem Monitor's query.
    """

    key: str
    label: str
    query: str
    goal: str
    monitor: str
    include_domains: tuple[str, ...] | None = None

    def render(self, company: str) -> dict[str, str]:
        """The query, goal and monitor description for one company."""
        return {
            "query": self.query.format(company=company),
            "goal": self.goal.format(company=company),
            "monitor": self.monitor.format(company=company),
        }


FIELDS: dict[str, FieldSpec] = {spec.key: spec for spec in (
    FieldSpec(
        key="ceo", label="CEO",
        query="{company} CEO",
        goal="find the full name of the current chief executive officer of {company}",
        monitor="{company} announces a new chief executive officer or its current CEO steps down.",
    ),
    FieldSpec(
        key="hq_city", label="HQ city",
        query="{company} headquarters city",
        goal="find the city where {company} has its headquarters today",
        monitor="{company} moves its headquarters to a different city.",
    ),
    FieldSpec(
        key="revenue", label="Latest fiscal year revenue",
        query="{company} annual revenue latest fiscal year",
        goal="find the total revenue {company} reported for its most recent fiscal year, with the year",
        monitor="{company} reports total annual revenue for a fiscal year newer than the one on file.",
    ),
    FieldSpec(
        key="funding", label="Latest funding round",
        query="{company} latest funding round",
        goal="find the most recent funding round {company} raised: series, amount and date",
        monitor="{company} announces a new funding round, acquisition or IPO.",
    ),
    FieldSpec(
        key="tenk_date", label="Latest 10-K filing date",
        query="{company} 10-K annual report filing date SEC",
        goal="find the date {company} filed its most recent annual report on Form 10-K with the SEC",
        monitor="{company} files a new annual report on Form 10-K with the SEC.",
        include_domains=("sec.gov",),
    ),
)}

DEFAULT_FIELDS: tuple[str, ...] = tuple(FIELDS)


def resolve_fields(keys: list[str] | tuple[str, ...] | None) -> list[FieldSpec]:
    """Look up field specs by key, in order; every field when ``keys`` is empty.

    Raises:
        ValueError: A key names no known field.
    """
    wanted = list(keys) if keys else list(DEFAULT_FIELDS)
    unknown = [key for key in wanted if key not in FIELDS]
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)}; known: {', '.join(FIELDS)}")
    return [FIELDS[key] for key in wanted]
