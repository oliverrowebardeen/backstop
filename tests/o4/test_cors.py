"""Cross-origin clients use the same authenticated race and public SSE routes."""
from fastapi.testclient import TestClient
import pytest

from server import api_v1
from server.app import ServerSettings, create_app
from server.usage import RateLimiter
from so import config
from tests.o4.access import AUTH, KEY
from tests.o4.fakes import run_question
from tests.o4.test_server import messages, root, settings

ORIGIN = "https://site.example"


def preflight(client: TestClient, origin: str = ORIGIN, method: str = "POST"):
    return client.options("/race" if method == "POST" else "/stream/example", headers={
        "Origin": origin,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": "Authorization, Content-Type" if method == "POST" else "Last-Event-ID",
    })


def test_cors_is_disabled_by_default_and_same_origin_replays_still_work(root):
    with TestClient(create_app(settings=settings(root, live_races=False))) as client:
        response = client.get("/replays", headers={"Origin": ORIGIN})
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers
        assert preflight(client).status_code == 405
        assert client.get("/replays").status_code == 200
        assert client.post("/race", headers=AUTH, json={"question": "Test"}).status_code == 403


def test_allowed_origins_preflight_without_starting_agents_and_reject_other_origins(root):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Preflight must never start an agent")

    with TestClient(create_app(settings=settings(root, cors_origins=(ORIGIN,)), runner=forbidden)) as client:
        for method in ("POST", "GET"):
            response = preflight(client, method=method)
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == ORIGIN
            assert "access-control-allow-credentials" not in response.headers
            assert "Origin" in response.headers["vary"]
        assert preflight(client, origin="https://other.example").status_code == 400
        assert preflight(client, method="DELETE").status_code == 400
        response = client.get("/replays", headers={"Origin": "https://other.example"})
        assert "access-control-allow-origin" not in response.headers
    assert not (root / "out/logs").exists()


def test_cors_preserves_live_gate_auth_and_shared_rate_limit(root, monkeypatch):
    monkeypatch.setenv("BACKSTOP_API_KEYS", KEY)
    app = create_app(settings=settings(root, cors_origins=(ORIGIN,)), runner=run_question)
    limiter = RateLimiter(limit=1)
    app.dependency_overrides[api_v1.get_rate_limiter] = lambda: limiter
    with TestClient(app, headers={"Origin": ORIGIN}) as client:
        denied = client.post("/race", json={"question": "Test"})
        assert denied.status_code == 401
        assert denied.headers["access-control-allow-origin"] == ORIGIN
        assert denied.headers["www-authenticate"] == "Bearer"
        assert "WWW-Authenticate" in denied.headers["access-control-expose-headers"]
        started = client.post("/race", headers=AUTH, json={"question": "Test"})
        assert started.status_code == 200
        assert started.headers["access-control-allow-origin"] == ORIGIN
        for run_id in started.json().values():
            stream = client.get(f"/stream/{run_id}")
            assert stream.headers["access-control-allow-origin"] == ORIGIN
            assert stream.headers["content-type"].startswith("text/event-stream")
            assert stream.headers["cache-control"] == "no-cache"
            rows = messages(stream)
            assert len([kind for kind, _ in rows if kind == "log"]) == 4
            assert rows[-1][0] == "complete"
            resumed = messages(client.get(f"/stream/{run_id}", headers={"Last-Event-ID": "3"}))
            assert [event["seq"] for kind, event in resumed if kind == "log"] == [4]
        limited = client.post("/v1/search", headers=AUTH, json={"query": "Test"})
        assert limited.status_code == 429
        assert limited.headers["access-control-allow-origin"] == ORIGIN
        assert "Retry-After" in limited.headers["access-control-expose-headers"]
    with TestClient(create_app(settings=settings(root, cors_origins=(ORIGIN,), live_races=False))) as client:
        denied = client.post("/race", headers={"Origin": ORIGIN, **AUTH}, json={"question": "Test"})
        assert denied.status_code == 403
        assert denied.headers["access-control-allow-origin"] == ORIGIN


async def no_wait(_: float) -> None:
    """Replay fixtures immediately while preserving the wire payloads."""


def test_cross_origin_replay_and_results_keep_their_public_payloads(root):
    with TestClient(create_app(settings=settings(root, cors_origins=(ORIGIN,), live_races=False),
                               replay_sleep=no_wait)) as client:
        for route in ("/replays", "/results", "/config", "/replay/fixture-ranked"):
            response = client.get(route, headers={"Origin": ORIGIN})
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == ORIGIN
            if route.startswith("/replay/"):
                rows = messages(response)
                assert rows[0][0] == "replay" and rows[0][1]["fixture"] is True
                assert sum(kind == "log" for kind, _ in rows) == 21
                assert rows[-1][0] == "complete"


@pytest.mark.parametrize("origin", ["*", "null", "site.example", "https://*.example", "https://site.example/",
                                    "https://site.example/path", "https://site.example?x=1", "https://site.example#x",
                                    "https://user:pass@site.example", "https://site.example:bad", "ftp://site.example"])
def test_cors_configuration_requires_exact_http_origins(root, origin):
    with pytest.raises(ValueError, match="SERVER_CORS_ORIGINS"):
        settings(root, cors_origins=(origin,))


def test_cors_settings_use_shared_configuration(monkeypatch):
    monkeypatch.setattr(config, "SERVER_CORS_ORIGINS", [ORIGIN, "http://localhost:3000"])
    assert ServerSettings.from_config().cors_origins == (ORIGIN, "http://localhost:3000")


def test_pitch_routes_and_assets_are_removed_but_reference_client_is_available(root):
    with TestClient(create_app(settings=settings(root))) as client:
        for route in ("/presentation", "/ui/proof.js", "/ui/fonts/LibreCaslonDisplay-Regular.ttf", "/ui/fonts/OFL.txt"):
            assert client.get(route).status_code == 404
        assert client.post("/race-stream", headers=AUTH, json={"question": "Test"}).status_code == 404
        page = client.get("/race")
        assert page.status_code == 200 and 'id="race-form"' in page.text
        assert 'id="export-audit"' in page.text
        assert 'id="proof"' not in page.text
        assert client.get("/ui/run-stream.js").status_code == 200
