"""Build a verified diligence fact sheet and keep it verified with Telem Monitors.

`FactSheetRunner.build` runs one Backstop search per field with an explicit goal, stores
the verdict (VERIFIED on HIT, else NOT_FOUND) with evidence, source, engines and cost,
then creates one daily Telem Monitor per field describing the change that would
invalidate it. `poll_all` reads every monitor, stores every item it returns, runs the
Backstop checker on each new item against the field's goal and stored value, and marks
the field STALE with the new evidence when an item contradicts or supersedes it.

Telem Monitors failures are logged with status and body and never stop the run.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from so.contracts import Result

from server.monitors.fields import FREQUENCIES, FieldSpec, resolve_fields
from server.monitors.gateway import MonitorItem, MonitorsError, MonitorsGateway
from server.monitors.store import FactSheetStore, now_iso

logger = logging.getLogger(__name__)

VERIFIED, NOT_FOUND, STALE = "VERIFIED", "NOT_FOUND", "STALE"
EVIDENCE_CHARS = 500
VALUE_CHARS = 200
#: Checks per item before a failing item is left as ERROR for good (each attempt is recorded).
MAX_CHECK_ATTEMPTS = 3

#: async (query, goal, session) -> a BackstopSearchResponse-shaped object.
SearchFn = Callable[[str, str, str | None], Awaitable[Any]]
#: async (query, goal, results) -> a CheckerVerdict-shaped object (verdict, why, cost_usd).
CheckFn = Callable[[str, str, list[Result]], Awaitable[Any]]
#: async (label, company, goal, evidence title, evidence snippet) -> the value, or None.
ExtractFn = Callable[[str, str, str, str, str], Awaitable[str | None]]


class FactSheetError(RuntimeError):
    """A fact sheet operation could not run (for example, no sheet for that company)."""


@dataclass
class PollReport:
    """What one `poll_all` did."""

    polled: int = 0
    failed: int = 0
    new_items: int = 0
    stale_fields: list[str] = field(default_factory=list)


@dataclass
class TeardownReport:
    """What one `teardown` did."""

    deleted: int = 0
    failed: int = 0


async def backstop_search(query: str, goal: str, session: str | None) -> Any:
    """One verified search through the public Backstop client."""
    from so.client import BackstopTelem

    async with BackstopTelem() as client:
        return await client.search(query, goal=goal, session=session)


async def backstop_check(query: str, goal: str, results: list[Result]) -> Any:
    """The Backstop checker on a list of results."""
    from so.checker import check

    return await check(query, goal, results)


_VALUE_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": ["string", "null"]}},
    "required": ["value"],
    "additionalProperties": False,
}
_VALUE_SYSTEM = (
    "You copy one fact out of a search result for a diligence fact sheet. Reply with JSON "
    '{"value": "..."}: the shortest phrase from the text that answers the goal, such as a name, '
    "a city, an amount with its fiscal year, a round with amount and date, or a date. Use only the "
    'text shown. If the text does not state it, reply {"value": null}.'
)


async def llm_extract_value(label: str, company: str, goal: str, title: str, snippet: str) -> str | None:
    """Pull the short value out of the checker's evidence result with the checker model."""
    import json

    from so import llm

    user = f"COMPANY: {company}\nFIELD: {label}\nGOAL: {goal}\nTITLE: {title}\nTEXT: {snippet}"
    resp = await llm.complete("checker", _VALUE_SYSTEM, [{"role": "user", "content": user}],
                              json_schema=_VALUE_SCHEMA)
    value = json.loads(resp.text or "{}").get("value")
    return str(value).strip()[:VALUE_CHARS] if value else None


def supersede_goal(label: str, company: str, value: str) -> str:
    """The checker goal for a monitor item: does it replace the value on file?"""
    return (f"find a {label} of {company} that replaces the value on file, \"{value}\": a different or "
            f"more recent {label} stated for {company}. A result that repeats the value on file, or "
            f"does not state a {label}, does not answer this goal")


def _evidence(response: Any) -> Any | None:
    results = list(getattr(response, "results", None) or [])
    for item in results:
        if getattr(item, "rank_reason", None) == "evidence":
            return item
    return results[0] if results else None


def _text(item: Any) -> str:
    text = getattr(item, "summary", None) or getattr(item, "snippet", None) or ""
    if not text and getattr(item, "excerpt", None):
        text = " ".join(item.excerpt)
    return " ".join(str(text).split())


class FactSheetRunner:
    """Builds, polls, renders and tears down fact sheets.

    Args:
        store: Where sheets live.
        search: Verified search; defaults to the public Backstop client.
        check: The checker; defaults to ``so.checker.check``.
        extract_value: Pulls the short value from the evidence; defaults to the checker model,
            falling back to the evidence snippet when it fails.
        monitors: The Telem Monitors gateway; built on first use when omitted.
        frequency: ``hourly``, ``daily`` or ``weekly``.
        webhook_base: Public https base URL of the webhook receiver. When set, each monitor
            delivers to ``{webhook_base}/monitors/webhook/{field_id}`` as well as by poll.
    """

    def __init__(self, store: FactSheetStore, *, search: SearchFn | None = None, check: CheckFn | None = None,
                 extract_value: ExtractFn | None = None, monitors: MonitorsGateway | None = None,
                 frequency: str = "daily", webhook_base: str | None = None) -> None:
        if frequency not in FREQUENCIES:
            raise ValueError(f"frequency must be one of {', '.join(FREQUENCIES)}")
        self.store = store
        self.search = search or backstop_search
        self.check = check or backstop_check
        self.extract_value = extract_value or llm_extract_value
        self._monitors = monitors
        self.frequency = frequency
        self.webhook_base = webhook_base.rstrip("/") if webhook_base else None

    @property
    def monitors(self) -> MonitorsGateway:
        """The Telem Monitors gateway, built from the environment on first use."""
        if self._monitors is None:
            self._monitors = MonitorsGateway()
        return self._monitors

    async def aclose(self) -> None:
        """Close the Telem client the gateway built, if any."""
        if self._monitors is not None:
            await self._monitors.aclose()

    # Build

    async def build(self, company: str, fields: list[str] | None = None, *,
                    with_monitors: bool = True) -> dict[str, Any]:
        """Search every field, store the sheet and create one monitor per field.

        An existing sheet for the company is replaced after its monitors are deleted.
        Returns the stored sheet as `sheet` renders it.
        """
        company = company.strip()
        if not company:
            raise ValueError("company must be a nonempty string")
        specs = resolve_fields(fields)
        existing = self.store.get_sheet(company)
        if existing is not None:
            logger.info("Replacing the existing fact sheet for %s", existing["company"])
            report = await self._delete_monitors(self.store.fields(existing["id"]))
            if report.failed:
                # Deleting the rows would lose the ids of monitors that keep running and billing.
                kept = [row["monitor_id"] for row in self.store.fields(existing["id"]) if row["monitor_id"]]
                raise FactSheetError(
                    f"rebuild of {existing['company']!r} aborted: {report.failed} Telem monitor(s) could not be "
                    f"deleted ({', '.join(kept)}); the sheet is kept, run teardown and build again")
            self.store.delete_sheet(existing["id"])
        sheet_id = self.store.create_sheet(company)
        session: str | None = None
        for spec in specs:
            field_id, session = await self._build_field(sheet_id, company, spec, session)
            if with_monitors:
                await self._create_monitor(field_id, company, spec)
        self.store.set_sheet_session(sheet_id, session)
        return self.sheet(company)

    async def _build_field(self, sheet_id: int, company: str, spec: FieldSpec,
                           session: str | None) -> tuple[int, str | None]:
        text = spec.render(company)
        row: dict[str, Any] = {"field": spec.key, "label": spec.label, "query": text["query"],
                               "goal": text["goal"], "verdict": NOT_FOUND, "verified_at": now_iso()}
        try:
            response = await self.search(text["query"], text["goal"], session)
        except Exception as exc:
            logger.error("Backstop search failed for %s %s: %s", company, spec.key, exc)
            row["search_error"] = f"{type(exc).__name__}: {exc}"
            return self.store.add_field(sheet_id, **row), session
        session = getattr(response, "session_id", None) or session
        row.update(engines_tried=list(getattr(response, "engines_called", None) or []),
                   cost_usd=float(getattr(response, "cost_usd", 0.0) or 0.0), telem_session_id=session)
        evidence = _evidence(response)
        if getattr(response, "status", None) == "HIT" and evidence is not None:
            snippet = _text(evidence)
            row.update(verdict=VERIFIED, evidence=snippet[:EVIDENCE_CHARS], source_url=evidence.url,
                       value=await self._value(spec.label, company, text["goal"], evidence.title or "", snippet))
        else:
            row["evidence"] = str(getattr(response, "why", "") or getattr(response, "note", ""))[:EVIDENCE_CHARS]
        return self.store.add_field(sheet_id, **row), session

    async def _value(self, label: str, company: str, goal: str, title: str, snippet: str) -> str:
        try:
            value = await self.extract_value(label, company, goal, title, snippet)
        except Exception as exc:
            logger.warning("Value extraction failed for %s %s, keeping the evidence snippet: %s",
                           company, label, exc)
            value = None
        return value or snippet[:VALUE_CHARS]

    async def _create_monitor(self, field_id: int, company: str, spec: FieldSpec) -> None:
        description = spec.render(company)["monitor"]
        domains = list(spec.include_domains) if spec.include_domains else None
        webhook = f"{self.webhook_base}/monitors/webhook/{field_id}" if self.webhook_base else None
        update: dict[str, Any] = {"monitor_description": description, "monitor_frequency": self.frequency,
                                  "include_domains": domains}
        try:
            created = await self.monitors.create(description, frequency=FREQUENCIES[self.frequency],
                                                 include_domains=domains, webhook_url=webhook)
        except MonitorsError as exc:
            update["monitor_error"] = f"create: status={exc.status} body={exc.body}"[:500]
        else:
            update.update(monitor_id=created.id, webhook_secret=created.webhook_secret, monitor_error=None)
        self.store.update_field(field_id, **update)

    # Poll

    async def poll_all(self, company: str | None = None) -> PollReport:
        """Poll every monitor (of one company, or of all), store and check what is new."""
        report = PollReport()
        for row in self._fields(company):
            if not row["monitor_id"]:
                continue
            try:
                items = await self.monitors.poll(row["monitor_id"])
            except MonitorsError as exc:
                report.failed += 1
                self.store.update_field(row["id"], monitor_error=f"poll: status={exc.status} body={exc.body}"[:500])
                continue
            report.polled += 1
            self.store.update_field(row["id"], last_polled_at=now_iso(), monitor_error=None)
            new, stale = await self.ingest(row["id"], items, via="poll")
            report.new_items += new
            if stale:
                report.stale_fields.append(f"{row['field']}#{row['id']}")
        return report

    async def ingest(self, field_id: int, items: list[MonitorItem], *, via: str) -> tuple[int, bool]:
        """Store items for one field and check each new one.

        Every stored item of the field without a verdict (new, or left by a crash) or whose
        check failed is checked too, up to MAX_CHECK_ATTEMPTS times each: Telem returns an item
        only once, so a failed check must not lose it.

        Returns how many items were new and whether the field went STALE.
        Raises:
            FactSheetError: No field has this id.
        """
        row = self.store.get_field(field_id)
        if row is None:
            raise FactSheetError(f"no fact sheet field with id {field_id}")
        company = self._company(row["sheet_id"])
        new = 0
        for item in items:
            item_id = self.store.add_item(field_id, monitor_id=row["monitor_id"], item_key=item.key, url=item.url,
                                          title=item.title, snippet=item.snippet, published=item.published, via=via)
            new += item_id is not None
        went_stale = False
        for stored in self.store.unchecked_items(field_id, MAX_CHECK_ATTEMPTS):
            item = MonitorItem(url=stored["url"] or "", title=stored["title"] or "", snippet=stored["snippet"] or "",
                               published=stored["published"], provider="telem_monitor")
            if await self._check_item(row, company, stored["id"], item, attempt=stored["check_attempts"] + 1):
                went_stale = True
                row = self.store.get_field(field_id) or row
        return new, went_stale

    async def _check_item(self, row: dict[str, Any], company: str, item_id: int, item: MonitorItem,
                          *, attempt: int) -> bool:
        if row["value"]:
            goal = supersede_goal(row["label"], company, row["value"])
        else:
            goal = row["goal"]  # NOT_FOUND: any item that answers the goal supersedes "not found"
        result = Result(rank=1, title=item.title or item.url or "untitled", url=item.url or "",
                        snippet=item.snippet, published=item.published, engine=item.provider)
        try:
            verdict = await self.check(row["query"], goal, [result])
        except Exception as exc:
            final = " (giving up)" if attempt >= MAX_CHECK_ATTEMPTS else ", will retry on the next poll or delivery"
            logger.error("Checker failed on monitor item %s for field %s, attempt %s of %s%s: %s",
                         item_id, row["id"], attempt, MAX_CHECK_ATTEMPTS, final, exc)
            self.store.update_item(item_id, check_verdict="ERROR", check_attempts=attempt,
                                   check_why=f"{type(exc).__name__}: {exc}"[:300])
            return False
        supersedes = verdict.verdict == "HIT"
        self.store.update_item(item_id, check_verdict=verdict.verdict, check_why=str(verdict.why)[:300],
                               check_attempts=attempt,
                               check_cost_usd=float(getattr(verdict, "cost_usd", 0.0) or 0.0),
                               supersedes=int(supersedes))
        if supersedes:
            logger.info("Field %s (%s) is STALE: %s", row["field"], company, item.url)
            self.store.update_field(row["id"], verdict=STALE, stale_evidence=item.snippet[:EVIDENCE_CHARS],
                                    stale_url=item.url, stale_why=str(verdict.why)[:300], stale_at=now_iso())
        return supersedes

    # Status, teardown, render

    async def set_status(self, company: str | None, status: str) -> int:
        """PATCH every monitor to ``active`` or ``paused``; returns how many succeeded."""
        if status not in {"active", "paused"}:
            raise ValueError("status must be active or paused")
        done = 0
        for row in self._fields(company):
            if not row["monitor_id"]:
                continue
            try:
                await self.monitors.update(row["monitor_id"], status=status)
            except MonitorsError as exc:
                self.store.update_field(row["id"], monitor_error=f"update: status={exc.status} body={exc.body}"[:500])
                continue
            done += 1
        return done

    async def teardown(self, company: str | None = None) -> TeardownReport:
        """Delete every monitor (of one company, or of all). The sheets stay readable."""
        return await self._delete_monitors(self._fields(company))

    async def _delete_monitors(self, rows: list[dict[str, Any]]) -> TeardownReport:
        report = TeardownReport()
        for row in rows:
            if not row["monitor_id"]:
                continue
            try:
                await self.monitors.delete(row["monitor_id"])
            except MonitorsError as exc:
                report.failed += 1
                self.store.update_field(row["id"], monitor_error=f"delete: status={exc.status} body={exc.body}"[:500])
                continue
            report.deleted += 1
            self.store.update_field(row["id"], monitor_id=None, webhook_secret=None)
        return report

    def sheet(self, company: str) -> dict[str, Any]:
        """One sheet with its fields and received items, without webhook secrets.

        Raises:
            FactSheetError: No sheet for this company.
        """
        sheet = self.store.get_sheet(company)
        if sheet is None:
            raise FactSheetError(f"no fact sheet for {company!r}; run build first")
        fields = []
        for row in self.store.fields(sheet["id"]):
            row = {key: value for key, value in row.items() if key != "webhook_secret"}
            row["items"] = self.store.items(row["id"])
            fields.append(row)
        return {**sheet, "fields": fields}

    def _fields(self, company: str | None) -> list[dict[str, Any]]:
        if company is None:
            return self.store.fields()
        sheet = self.store.get_sheet(company)
        if sheet is None:
            raise FactSheetError(f"no fact sheet for {company!r}; run build first")
        return self.store.fields(sheet["id"])

    def _company(self, sheet_id: int) -> str:
        for sheet in self.store.sheets():
            if sheet["id"] == sheet_id:
                return str(sheet["company"])
        raise FactSheetError(f"no fact sheet with id {sheet_id}")


def render_text(sheet: dict[str, Any]) -> str:
    """A plain-text fact sheet: one block per field, STALE fields with the new evidence."""
    lines = [f"Fact sheet: {sheet['company']}  (Telem session {sheet.get('telem_session_id') or '-'})", ""]
    for row in sheet["fields"]:
        lines.append(f"{row['label']}: {row['value'] or '-'}  [{row['verdict']}]")
        if row.get("source_url"):
            lines.append(f"  source: {row['source_url']}")
        if row.get("evidence"):
            lines.append(f"  evidence: {row['evidence'][:200]}")
        engines = ", ".join(row.get("engines_tried") or []) or "-"
        lines.append(f"  engines: {engines}  cost: ${row.get('cost_usd') or 0:.4f}")
        if row.get("search_error"):
            lines.append(f"  search error: {row['search_error']}")
        monitor = row.get("monitor_id") or "none"
        lines.append(f"  monitor: {monitor} ({row.get('monitor_frequency') or '-'}), "
                     f"{len(row.get('items') or [])} item(s) received, last polled {row.get('last_polled_at') or 'never'}")
        if row.get("monitor_error"):
            lines.append(f"  monitor error: {row['monitor_error'][:200]}")
        if row["verdict"] == STALE:
            lines.append(f"  STALE since {row['stale_at']}: {row.get('stale_url') or '-'}")
            lines.append(f"    new evidence: {(row.get('stale_evidence') or '')[:200]}")
            lines.append(f"    why: {row.get('stale_why') or '-'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
