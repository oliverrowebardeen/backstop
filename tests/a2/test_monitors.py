"""Fact sheet build, poll, webhook and teardown against fake Telem Monitors and search."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from server.monitors import FactSheetRunner, FactSheetStore, MonitorsGateway, render_text
from server.monitors.webhook import create_app
from server.monitors.__main__ import main as cli_main

from tests.a2.fakes import FakeCheck, FakeMonitorsAPI, FakeSearch, fake_extract, hit, miss

COMPANY = "Acme Robotics"


@pytest.fixture
def store(tmp_path):
    s = FactSheetStore(tmp_path / "factsheets.db")
    yield s
    s.close()


def answers():
    return {
        "CEO": hit("https://acme.example/leadership", "Jane Doe is the CEO of Acme Robotics. She joined in 2019."),
        "headquarters": hit("https://acme.example/about", "Acme Robotics is headquartered in Austin. Founded 2015."),
        "10-K": hit("https://www.sec.gov/acme-10k", "Acme filed its 10-K on 2026-02-27. Annual report."),
        "revenue": miss(),
        "funding": RuntimeError("Telem unavailable"),
    }


def make_runner(store, api=None, check=None, search=None, **kwargs):
    return FactSheetRunner(store, search=search or FakeSearch(answers()), check=check or FakeCheck(("John Roe",)),
                           extract_value=fake_extract, monitors=MonitorsGateway(api or FakeMonitorsAPI()), **kwargs)


def by_field(sheet):
    return {row["field"]: row for row in sheet["fields"]}


async def test_build_stores_sheet_and_one_monitor_per_field(store):
    api = FakeMonitorsAPI()
    search = FakeSearch(answers())
    runner = make_runner(store, api, search=search)
    sheet = await runner.build(COMPANY)
    rows = by_field(sheet)

    assert list(rows) == ["ceo", "hq_city", "revenue", "funding", "tenk_date"]
    ceo = rows["ceo"]
    assert ceo["verdict"] == "VERIFIED" and ceo["value"] == "Jane Doe is the CEO of Acme Robotics"
    assert ceo["source_url"] == "https://acme.example/leadership" and "Jane Doe" in ceo["evidence"]
    assert ceo["engines_tried"] == ["tavily"] and ceo["cost_usd"] == pytest.approx(0.0021)
    assert ceo["telem_session_id"] == "sess-1" and sheet["telem_session_id"] == "sess-1"
    assert rows["revenue"]["verdict"] == "NOT_FOUND" and rows["revenue"]["value"] is None
    assert rows["revenue"]["engines_tried"] == ["tavily", "exa", "brave"]
    assert rows["funding"]["verdict"] == "NOT_FOUND" and "Telem unavailable" in rows["funding"]["search_error"]

    # Every search has an explicit goal; the first session is reused for the rest.
    assert all(goal and goal != query for query, goal, _ in search.calls)
    assert [session for *_, session in search.calls] == [None, "sess-1", "sess-1", "sess-1", "sess-1"]

    creates = [call for call in api.calls if call[0] == "create"]
    assert len(creates) == 5
    assert all(call[2]["frequency"] == "1d" and call[2]["agent"] is False for call in creates)
    assert all(call[1].endswith(".") and COMPANY in call[1] for call in creates)
    tenk = next(call for call in creates if "10-K" in call[1])
    assert tenk[2]["include_domains"] == ["sec.gov"]
    assert "include_domains" not in next(call for call in creates if "chief executive" in call[1])[2]
    assert all(row["monitor_id"] and row["monitor_frequency"] == "daily" for row in rows.values())
    assert rows["tenk_date"]["include_domains"] == ["sec.gov"]


async def test_poll_with_new_superseding_item_marks_field_stale(store):
    api = FakeMonitorsAPI()
    check = FakeCheck(("John Roe",))
    runner = make_runner(store, api, check=check)
    await runner.build(COMPANY)
    ceo = by_field(runner.sheet(COMPANY))["ceo"]
    api.queued[ceo["monitor_id"]] = [
        {"url": "https://news.example/acme-ceo", "title": "Acme names John Roe CEO",
         "summary": "Acme Robotics appointed John Roe as chief executive, replacing Jane Doe.",
         "publish_date": "2026-11-13"},
        {"url": "https://news.example/acme-office", "title": "Acme opens office",
         "summary": "Acme Robotics opened a new office."},
    ]
    report = await runner.poll_all()

    assert report.polled == 5 and report.failed == 0 and report.new_items == 2
    assert report.stale_fields == [f"ceo#{ceo['id']}"]
    ceo = by_field(runner.sheet(COMPANY))["ceo"]
    assert ceo["verdict"] == "STALE"
    assert ceo["value"] == "Jane Doe is the CEO of Acme Robotics"  # the old value is kept for the record
    assert ceo["stale_url"] == "https://news.example/acme-ceo" and "John Roe" in ceo["stale_evidence"]
    assert ceo["stale_at"] and ceo["stale_why"] and ceo["last_polled_at"]
    assert [item["supersedes"] for item in ceo["items"]] == [1, 0]
    assert [item["via"] for item in ceo["items"]] == ["poll", "poll"]

    # The checker saw the stored value in its goal and the field's query.
    query, goal, results = check.calls[0]
    assert query == "Acme Robotics CEO" and "Jane Doe is the CEO of Acme Robotics" in goal
    assert results[0].url == "https://news.example/acme-ceo" and results[0].published == "2026-11-13"

    text = render_text(runner.sheet(COMPANY))
    assert "[STALE]" in text and "https://news.example/acme-ceo" in text


async def test_poll_with_no_news_changes_nothing(store):
    api = FakeMonitorsAPI()
    check = FakeCheck(("John Roe",))
    runner = make_runner(store, api, check=check)
    await runner.build(COMPANY)
    before = by_field(runner.sheet(COMPANY))
    report = await runner.poll_all(COMPANY)
    after = by_field(runner.sheet(COMPANY))

    assert report.polled == 5 and report.new_items == 0 and report.stale_fields == []
    assert check.calls == []
    assert {key: row["verdict"] for key, row in after.items()} == {key: row["verdict"] for key, row in before.items()}
    assert all(row["items"] == [] for row in after.values())


async def test_repeated_item_is_stored_and_checked_once(store):
    api = FakeMonitorsAPI()
    check = FakeCheck()
    runner = make_runner(store, api, check=check)
    await runner.build(COMPANY, ["ceo"])
    monitor_id = by_field(runner.sheet(COMPANY))["ceo"]["monitor_id"]
    item = {"url": "https://news.example/same", "summary": "Jane Doe spoke at a conference."}
    api.queued[monitor_id] = [item]
    await runner.poll_all()
    api.queued[monitor_id] = [item]
    report = await runner.poll_all()
    assert report.new_items == 0 and len(check.calls) == 1


async def test_not_found_field_goes_stale_when_an_item_answers_its_goal(store):
    api = FakeMonitorsAPI()
    runner = make_runner(store, api, check=FakeCheck(("$12 million",)))
    await runner.build(COMPANY, ["revenue"])
    row = by_field(runner.sheet(COMPANY))["revenue"]
    api.queued[row["monitor_id"]] = [{"url": "https://ir.example/fy26", "summary": "FY2026 revenue was $12 million."}]
    await runner.poll_all()
    assert by_field(runner.sheet(COMPANY))["revenue"]["verdict"] == "STALE"


async def test_monitor_failures_log_status_and_body_and_continue(store, caplog):
    caplog.set_level(logging.ERROR)
    api = FakeMonitorsAPI(fail={"create": 503})
    runner = make_runner(store, api)
    sheet = await runner.build(COMPANY)
    rows = by_field(sheet)
    assert all(row["monitor_id"] is None for row in rows.values())
    assert all("status=503" in row["monitor_error"] for row in rows.values())
    assert rows["ceo"]["verdict"] == "VERIFIED"  # the sheet still builds
    assert "status=503" in caplog.text and "create broke" in caplog.text
    assert "monitor error: create: status=503" in render_text(sheet)

    api.fail = {"poll": 500}
    await runner.build(COMPANY)  # replaces the sheet, now with monitors
    report = await runner.poll_all()
    assert report.polled == 0 and report.failed == 5
    assert "status=500" in caplog.text
    assert render_text(runner.sheet(COMPANY))


async def test_teardown_deletes_every_monitor_and_keeps_the_sheet(store):
    api = FakeMonitorsAPI()
    runner = make_runner(store, api)
    await runner.build(COMPANY)
    report = await runner.teardown(COMPANY)
    assert report.deleted == 5 and report.failed == 0 and api.monitors == {}
    rows = by_field(runner.sheet(COMPANY))
    assert all(row["monitor_id"] is None for row in rows.values())
    assert rows["ceo"]["verdict"] == "VERIFIED"
    assert (await runner.poll_all()).polled == 0


async def test_teardown_failure_keeps_the_monitor_id(store):
    api = FakeMonitorsAPI()
    runner = make_runner(store, api)
    await runner.build(COMPANY, ["ceo"])
    api.fail = {"delete": 502}
    report = await runner.teardown()
    row = by_field(runner.sheet(COMPANY))["ceo"]
    assert report.failed == 1 and row["monitor_id"] and "status=502" in row["monitor_error"]


async def test_rebuild_deletes_old_monitors(store):
    api = FakeMonitorsAPI()
    runner = make_runner(store, api)
    await runner.build(COMPANY, ["ceo"])
    await runner.build(COMPANY.lower(), ["ceo"])
    assert len(api.monitors) == 1 and len(store.sheets()) == 1


async def test_pause_and_resume_patch_every_monitor(store):
    api = FakeMonitorsAPI()
    runner = make_runner(store, api)
    await runner.build(COMPANY, ["ceo", "hq_city"])
    assert await runner.set_status(COMPANY, "paused") == 2
    assert all(monitor["status"] == "paused" for monitor in api.monitors.values())


def _sign(body: bytes, secret: str, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


async def _webhook_setup(store):
    api = FakeMonitorsAPI()
    runner = make_runner(store, api, webhook_base="https://hooks.example/")
    await runner.build(COMPANY, ["ceo"])
    row = by_field(runner.sheet(COMPANY))["ceo"]
    create = next(call for call in api.calls if call[0] == "create")
    assert create[2]["webhook_url"] == f"https://hooks.example/monitors/webhook/{row['id']}"
    assert "webhook_secret" not in row  # never rendered
    return runner, row


async def test_webhook_with_valid_signature_ingests_and_marks_stale(store):
    runner, row = await _webhook_setup(store)
    client = TestClient(create_app(runner))
    body = json.dumps({"monitor_id": row["monitor_id"], "results": [
        {"url": "https://news.example/roe", "title": "New CEO", "summary": "John Roe is now CEO of Acme Robotics."},
    ]}).encode()
    resp = client.post(f"/monitors/webhook/{row['id']}", content=body,
                       headers={"Telem-Signature": _sign(body, "whsec_test"), "Content-Type": "application/json"})
    assert resp.status_code == 200 and resp.json() == {"field_id": row["id"], "new_items": 1, "stale": True}
    ceo = by_field(runner.sheet(COMPANY))["ceo"]
    assert ceo["verdict"] == "STALE" and ceo["items"][0]["via"] == "webhook"


@pytest.mark.parametrize("header", [None, "t=1,v1=deadbeef", "garbage", "SIGN_WRONG_SECRET", "SIGN_OLD"])
async def test_webhook_with_invalid_signature_is_rejected(store, header):
    runner, row = await _webhook_setup(store)
    client = TestClient(create_app(runner))
    body = json.dumps({"results": [{"url": "https://news.example/roe", "summary": "John Roe is CEO."}]}).encode()
    if header == "SIGN_WRONG_SECRET":
        header = _sign(body, "whsec_other")
    elif header == "SIGN_OLD":
        header = _sign(body, "whsec_test", ts=int(time.time()) - 3600)
    headers = {"Telem-Signature": header} if header else {}
    resp = client.post(f"/monitors/webhook/{row['id']}", content=body, headers=headers)
    assert resp.status_code == 401
    assert by_field(runner.sheet(COMPANY))["ceo"]["items"] == []


async def test_webhook_unknown_field_is_404(store):
    runner, _ = await _webhook_setup(store)
    client = TestClient(create_app(runner))
    assert client.post("/monitors/webhook/999", content=b"{}").status_code == 404


def test_cli_show_and_unknown_company(tmp_path, capsys):
    db = tmp_path / "f.db"
    assert cli_main(["--db", str(db), "show"]) == 0
    assert "no fact sheets" in capsys.readouterr().out
    assert cli_main(["--db", str(db), "show", "--company", "Nobody"]) == 2


def test_cli_build_poll_show_teardown_with_fakes(tmp_path, capsys, monkeypatch):
    api = FakeMonitorsAPI()
    import server.monitors.__main__ as cli

    class Runner(FactSheetRunner):
        def __init__(self, store, **kwargs):
            super().__init__(store, search=FakeSearch(answers()), check=FakeCheck(), extract_value=fake_extract,
                             monitors=MonitorsGateway(api), **kwargs)

    monkeypatch.setattr(cli, "FactSheetRunner", Runner)
    db = str(tmp_path / "f.db")
    assert cli_main(["--db", db, "build", "--company", COMPANY, "--fields", "ceo", "tenk_date"]) == 0
    assert "Jane Doe" in capsys.readouterr().out
    assert cli_main(["--db", db, "poll"]) == 0
    assert "polled 2" in capsys.readouterr().out
    assert cli_main(["--db", db, "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["company"] == COMPANY
    assert cli_main(["--db", db, "teardown"]) == 0
    assert "deleted 2" in capsys.readouterr().out and api.monitors == {}


class FlakyCheck(FakeCheck):
    """Raises (an outage or a budget stop) for the first `failures` calls, then behaves."""

    def __init__(self, failures: int, hit_words: tuple[str, ...] = ("John Roe",)) -> None:
        super().__init__(hit_words)
        self.failures = failures

    async def __call__(self, query, goal, results):
        if self.failures > 0:
            self.failures -= 1
            self.calls.append((query, goal, results))
            raise RuntimeError("Budget exceeded: LLM_BUDGET_USD")
        return await super().__call__(query, goal, results)


ROE = {"url": "https://news.example/acme-ceo", "title": "Acme names John Roe CEO",
       "summary": "Acme Robotics appointed John Roe as chief executive."}


async def test_item_whose_check_failed_is_rechecked_on_the_next_poll(store):
    api = FakeMonitorsAPI()
    check = FlakyCheck(failures=1)
    runner = make_runner(store, api, check=check)
    await runner.build(COMPANY, ["ceo"])
    monitor_id = by_field(runner.sheet(COMPANY))["ceo"]["monitor_id"]
    api.queued[monitor_id] = [ROE]
    first = await runner.poll_all()
    ceo = by_field(runner.sheet(COMPANY))["ceo"]
    assert first.stale_fields == [] and ceo["verdict"] == "VERIFIED"
    assert ceo["items"][0]["check_verdict"] == "ERROR" and ceo["items"][0]["check_attempts"] == 1

    api.queued[monitor_id] = [ROE]  # Telem redelivers it, or delivers nothing: either way it is rechecked
    second = await runner.poll_all()
    ceo = by_field(runner.sheet(COMPANY))["ceo"]
    assert second.new_items == 0 and second.stale_fields == [f"ceo#{ceo['id']}"]
    assert ceo["verdict"] == "STALE" and ceo["stale_url"] == ROE["url"]
    assert ceo["items"][0]["check_verdict"] == "HIT" and ceo["items"][0]["check_attempts"] == 2


async def test_item_left_unchecked_by_a_crash_is_rechecked(store):
    api = FakeMonitorsAPI()
    runner = make_runner(store, api)
    await runner.build(COMPANY, ["ceo"])
    row = by_field(runner.sheet(COMPANY))["ceo"]
    # A crash between add_item and the check leaves the item stored with no verdict.
    store.add_item(row["id"], monitor_id=row["monitor_id"], item_key=ROE["url"], url=ROE["url"],
                   title=ROE["title"], snippet=ROE["summary"], published=None, via="poll")
    report = await runner.poll_all()
    assert report.stale_fields == [f"ceo#{row['id']}"]
    assert by_field(runner.sheet(COMPANY))["ceo"]["verdict"] == "STALE"


async def test_recheck_retries_are_bounded_and_recorded(store):
    from server.monitors.runner import MAX_CHECK_ATTEMPTS

    api = FakeMonitorsAPI()
    check = FlakyCheck(failures=100)
    runner = make_runner(store, api, check=check)
    await runner.build(COMPANY, ["ceo"])
    api.queued[by_field(runner.sheet(COMPANY))["ceo"]["monitor_id"]] = [ROE]
    for _ in range(MAX_CHECK_ATTEMPTS + 3):
        await runner.poll_all()
    item = by_field(runner.sheet(COMPANY))["ceo"]["items"][0]
    assert len(check.calls) == MAX_CHECK_ATTEMPTS
    assert item["check_attempts"] == MAX_CHECK_ATTEMPTS and item["check_verdict"] == "ERROR"
    assert "Budget exceeded" in item["check_why"]


async def test_webhook_delivery_rechecks_failed_items(store):
    runner, row = await _webhook_setup(store)
    runner.check = FlakyCheck(failures=1)
    client = TestClient(create_app(runner))
    body = json.dumps({"results": [ROE]}).encode()
    headers = {"Telem-Signature": _sign(body, "whsec_test")}
    assert client.post(f"/monitors/webhook/{row['id']}", content=body, headers=headers).json()["stale"] is False
    body = json.dumps({"results": [ROE]}).encode()
    headers = {"Telem-Signature": _sign(body, "whsec_test")}
    resp = client.post(f"/monitors/webhook/{row['id']}", content=body, headers=headers).json()
    assert resp == {"field_id": row["id"], "new_items": 0, "stale": True}


async def test_rebuild_aborts_when_an_old_monitor_cannot_be_deleted(store):
    from server.monitors import FactSheetError

    api = FakeMonitorsAPI()
    runner = make_runner(store, api)
    await runner.build(COMPANY, ["ceo", "hq_city"])
    old = by_field(runner.sheet(COMPANY))
    api.fail = {"delete": 503}
    with pytest.raises(FactSheetError) as err:
        await runner.build(COMPANY, ["ceo"])
    assert old["ceo"]["monitor_id"] in str(err.value) and old["hq_city"]["monitor_id"] in str(err.value)
    kept = by_field(runner.sheet(COMPANY))
    assert {key: row["monitor_id"] for key, row in kept.items()} == {key: row["monitor_id"] for key, row in old.items()}
    assert all("status=503" in row["monitor_error"] for row in kept.values())
    assert len([call for call in api.calls if call[0] == "create"]) == 2  # nothing new was created

    api.fail = {}
    await runner.teardown(COMPANY)  # teardown still finds them
    assert api.monitors == {}
