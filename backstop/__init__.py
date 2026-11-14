"""Backstop: verified search on top of Telem.

    from backstop import BackstopTelem

    async with BackstopTelem() as client:     # drop-in for telem.AsyncTelem
        resp = await client.search("query", goal="what the agent needs")

The implementation lives in the `so` package; this module is the stable public name.
"""

from so.client import (
    BackstopEvidence,
    BackstopRankEntry,
    BackstopProviderRun,
    BackstopResult,
    BackstopSearchResponse,
    BackstopTelem,
    SearchError,
    SearchLimits,
    TelemUnavailable,
)

__version__ = "0.1.0"

__all__ = [
    "BackstopEvidence",
    "BackstopRankEntry",
    "BackstopProviderRun",
    "BackstopResult",
    "BackstopSearchResponse",
    "BackstopTelem",
    "SearchError",
    "SearchLimits",
    "TelemUnavailable",
    "__version__",
]
