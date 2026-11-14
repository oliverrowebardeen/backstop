"""Retrieval history scoped to one agent run."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterable

from so.contracts import Result


@dataclass
class RunMemory:
    """Keep issued queries, their engines, and observed URLs across searches."""

    queries: list[str] = field(default_factory=list)
    engines_by_query: dict[str, set[str]] = field(default_factory=dict)
    urls_seen: set[str] = field(default_factory=set)

    def record_search(self, query: str, engine: str, results: Iterable[Result]) -> None:
        self.queries.append(query)
        self.engines_by_query.setdefault(query, set()).add(engine)
        self.urls_seen.update(result.url for result in results)

    def record_fetch(self, url: str) -> None:
        self.urls_seen.add(url)
