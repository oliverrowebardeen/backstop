"""S7 product site: routes, fixtures and honesty labels."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from server.app import ServerSettings, create_app

ROOT = Path(__file__).resolve().parents[2]


def _client(tmp_path: Path, frontier: bool = True) -> TestClient:
    out = tmp_path / "out"
    out.mkdir()
    if frontier:
        (out / "frontier.json").write_text((ROOT / "out/frontier.json").read_text())
        (out / "rescue_table.json").write_text((ROOT / "out/rescue_table.json").read_text())
    settings = ServerSettings(ui_dir=ROOT / "ui", demo_dir=ROOT / "demo", fixture_dir=None, log_dir=tmp_path / "logs",
                              results_path=out / "results.json", frontier_path=out / "frontier.json",
                              max_active_runs=2, max_question_chars=500, start_timeout=1, run_timeout=1,
                              poll_seconds=0.01, keepalive_seconds=1)
    return TestClient(create_app(settings=settings))


def test_home_race_and_site_data(tmp_path):
    with _client(tmp_path) as client:
        home = client.get("/").text
        assert 'id="simulate"' not in home and 'Simulate a miss' not in home
        for gone in ('id="memo"', 'id="autopsy"', "memo.js", "memo.css", "Miss autopsy", "Check a memo"):
            assert gone not in home, gone
        for path in ("/ui/memo.js", "/ui/memo-view.js", "/ui/memo.css", "/ui/fixtures/memos.json"):
            assert client.get(path).status_code == 404, path
        for anchor in ('id="race"', 'id="how"', 'id="results"', 'id="developers"', 'href="#race"'):
            assert anchor in home
        assert 'id="race-form"' in home and 'src="/ui/race.js"' in home
        moved = client.get("/race", follow_redirects=False)
        assert moved.status_code == 307 and moved.headers["location"] == "/#race"
        assert 'id="race-form"' in client.get("/race").text
        data = client.get("/site-data").json()
        assert data["rescue_table"]["built_on_split"] == "learn"
        so = [p for p in data["frontier"]["points"] if p["kind"] == "second_opinion"]
        assert all("simulated" in p for p in so if json.loads((ROOT / "out/frontier.json").read_text()))
        assert client.get("/results").json()["results"] is None


def test_site_data_without_artifacts(tmp_path):
    with _client(tmp_path, frontier=False) as client:
        data = client.get("/site-data").json()
        assert data["rescue_table"] is None and data["frontier"] is None


def test_fixtures_are_served_and_secret_free(tmp_path):
    with _client(tmp_path) as client:
        for manifest in sorted((ROOT / "ui/fixtures").glob("race-*.json")):
            item = json.loads(manifest.read_text())
            for name in (item["left"], item["right"]):
                text = client.get(f"/ui/fixtures/{name}").text
                rows = [json.loads(line) for line in text.splitlines() if line.strip()]
                assert rows and rows[0]["type"] == "run_start"
                lowered = text.lower()
                for marker in ("api_key", "authorization", "bearer ", "sk-ant", "tvly-", "bk_live"):
                    assert marker not in lowered, (name, marker)


def test_developer_curl_matches_the_hosted_api():
    """The site's curl example uses the real route, auth header and request fields of server/api_v1.py."""
    import re

    from server import api_v1

    html = (ROOT / "ui/index.html").read_text()
    curl = re.search(r'<pre id="code-curl"><code>(.*?)</code></pre>', html, re.S).group(1)
    # app.js swaps the base URL for the page's own origin; the shipped default is the deployed site.
    assert '<span class="api-base">https://backstop-pied.vercel.app</span>/v1/search' in curl
    curl = re.sub(r"<[^>]+>", "", curl)
    assert re.search(r"curl -s https://\S+/v1/search \\", curl) and "localhost" not in curl
    routes = {(route.path, method) for route in api_v1.router.routes for method in getattr(route, "methods", ())}
    assert ("/v1/search", "POST") in routes and " -d " in curl  # -d makes curl send a POST
    assert '-H "Authorization: Bearer $BACKSTOP_KEY"' in curl
    body = re.search(r"-d '(.*)'", curl, re.S).group(1).replace("'\\''", "'")
    api_v1.SearchRequest.model_validate_json(body)  # extra fields are forbidden, so this checks every field
