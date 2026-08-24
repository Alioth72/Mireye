"""Tests for the console server: config exposure, health degradation, proxying.

No network. Every upstream call goes through an ``httpx.MockTransport`` that is
installed on ``app.state.http`` before the lifespan runs - ``lifespan()`` adopts
a pre-installed client instead of dialling out.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

# The server modules live in frontend/, one level up from tests/.
FRONTEND_DIR = Path(__file__).resolve().parents[1]
if str(FRONTEND_DIR) not in sys.path:
    sys.path.insert(0, str(FRONTEND_DIR))

from app import create_app  # noqa: E402
from config import Settings, get_settings  # noqa: E402

BACKEND = "http://backend.test"


def _settings(**overrides) -> Settings:
    """Settings built from explicit kwargs.

    Init kwargs outrank both the process environment and any local ``.env``,
    so these tests are unaffected by whatever the developer has configured.
    """
    base = dict(
        backend_base_url=BACKEND,
        google_maps_browser_key="",
        google_maps_map_id_light="",
        google_maps_map_id_dark="",
    )
    base.update(overrides)
    return Settings(**base)


@contextmanager
def console(handler, settings: Settings | None = None):
    """A TestClient whose upstream is ``handler``, with the calls it recorded."""
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings or _settings()
    app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=BACKEND,
    )
    try:
        with TestClient(app) as client:
            yield client, calls
    finally:
        app.dependency_overrides.clear()


def _json(payload, status: int = 200):
    return lambda request: httpx.Response(status, json=payload)


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


# ---------------------------------------------------------------------------
# /api/config - the key never lives in the bundle
# ---------------------------------------------------------------------------
def test_config_omits_key_when_unset():
    with console(_json({}), settings=_settings()) as (client, _):
        body = client.get("/api/config").json()

    assert body["maps_key"] is None, "an unset key must be null, not empty string"
    assert body["map_id_light"] is None
    assert body["map_id_dark"] is None
    assert body["backend_base_url"] == BACKEND
    # Nothing key-shaped leaked under another name.
    assert not any("AIza" in str(v) for v in body.values())


def test_config_includes_key_when_set():
    cfg = _settings(
        google_maps_browser_key="AIzaTESTKEY",
        google_maps_map_id_light="light-map-id",
        google_maps_map_id_dark="dark-map-id",
    )
    with console(_json({}), settings=cfg) as (client, _):
        body = client.get("/api/config").json()

    assert body["maps_key"] == "AIzaTESTKEY"
    assert body["map_id_light"] == "light-map-id"
    assert body["map_id_dark"] == "dark-map-id"


def test_maps_key_is_not_present_in_any_static_file():
    """The key is injected at request time; nothing under static/ may hold one."""
    for path in (FRONTEND_DIR / "static").rglob("*"):
        if path.is_file() and path.suffix in {".js", ".html", ".css", ".json"}:
            assert "AIza" not in path.read_text(encoding="utf-8", errors="ignore"), path


# ---------------------------------------------------------------------------
# /api/health - deliberately never raises
# ---------------------------------------------------------------------------
def test_health_reports_not_ok_when_backend_is_down():
    with console(_unreachable) as (client, calls):
        res = client.get("/api/health")

    assert res.status_code == 200, "a dead backend must not break the console at boot"
    body = res.json()
    assert body["ok"] is False
    assert body["error"]
    assert calls[0].url.path == "/v1/healthz"


def test_health_passes_backend_payload_through_when_up():
    with console(_json({"ok": True, "db": "phase2.db"})) as (client, calls):
        body = client.get("/api/health").json()

    assert body["ok"] is True
    assert body["db"] == "phase2.db"
    assert calls[0].url.path == "/v1/healthz"


def test_health_reports_not_ok_on_backend_error_status():
    with console(_json({"detail": "boom"}, status=503)) as (client, _):
        res = client.get("/api/health")

    assert res.status_code == 200
    assert res.json()["ok"] is False


# ---------------------------------------------------------------------------
# GET proxying - path rewrite, query string, body pass-through
# ---------------------------------------------------------------------------
def test_get_proxy_forwards_path_and_query_and_body():
    payload = [{"canonical_id": "sea-2024-001", "stage": "ADOPTED"}]
    with console(_json(payload)) as (client, calls):
        res = client.get("/api/events", params={"stage": "ADOPTED", "limit": 5})

    assert res.status_code == 200
    assert res.json() == payload

    sent = calls[0]
    assert sent.method == "GET"
    assert sent.url.path == "/phase1/events", "/api/events maps to /phase1/events"
    assert dict(sent.url.params) == {"stage": "ADOPTED", "limit": "5"}


def test_bundle_path_is_rewritten_to_the_backend_shape():
    """The browser says /bundle/<name>; the backend hangs it off the site."""
    with console(_json({"status": "ok"})) as (client, calls):
        client.get("/api/sites/site-7/bundle/terrain")

    assert calls[0].url.path == "/v1/sites/site-7/terrain"


def test_event_detail_path_is_forwarded():
    with console(_json({"canonical_id": "sea-2024-001"})) as (client, calls):
        client.get("/api/events/sea-2024-001")

    assert calls[0].url.path == "/phase1/events/sea-2024-001"


# ---------------------------------------------------------------------------
# POST proxying - JSON body survives the hop
# ---------------------------------------------------------------------------
def test_post_proxy_forwards_json_body():
    body = {"name": "Quincy DC", "lat": 47.234, "lon": -119.852}

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == body
        return httpx.Response(201, json={"site_id": "site-9", **body})

    with console(handler) as (client, calls):
        res = client.post("/api/sites", json=body)

    assert res.status_code == 201
    assert res.json()["site_id"] == "site-9"

    sent = calls[0]
    assert sent.method == "POST"
    assert sent.url.path == "/v1/sites"
    assert json.loads(sent.content) == body


def test_decide_posts_to_v1_decide():
    payload = {"event_id": "sea-2024-001", "site_id": "site-9"}

    with console(_json({"decision": "ALERT"})) as (client, calls):
        res = client.post("/api/decide", json=payload)

    assert res.status_code == 200
    assert calls[0].url.path == "/v1/decide"
    assert json.loads(calls[0].content) == payload


# ---------------------------------------------------------------------------
# errors are not swallowed
# ---------------------------------------------------------------------------
def test_backend_404_surfaces_as_404():
    with console(_json({"detail": "no such site"}, status=404)) as (client, _):
        res = client.get("/api/sites/nope")

    assert res.status_code == 404, "a 404 must not be laundered into a 200"
    assert res.json()["detail"] == "no such site"


def test_unimplemented_vicinity_route_passes_the_404_through():
    """vicinity/derived do not exist backend-side yet - no special-casing."""
    with console(_json({"detail": "Not Found"}, status=404)) as (client, calls):
        res = client.get("/api/sites/site-7/vicinity")

    assert res.status_code == 404
    assert calls[0].url.path == "/v1/sites/site-7/vicinity"


def test_backend_500_surfaces_as_500():
    with console(_json({"detail": "kaboom"}, status=500)) as (client, _):
        res = client.get("/api/budget")

    assert res.status_code == 500
    assert res.json()["detail"] == "kaboom"


def test_unreachable_backend_is_a_502_not_a_200():
    with console(_unreachable) as (client, _):
        res = client.get("/api/sites")

    assert res.status_code == 502
    assert res.json()["detail"]["error"] == "backend_unreachable"


# ---------------------------------------------------------------------------
# the shell
# ---------------------------------------------------------------------------
def test_root_serves_index_html():
    with console(_json({})) as (client, _):
        res = client.get("/")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "The Monitor" in res.text
    assert "/static/js/app.js" in res.text


def test_static_files_are_mounted():
    with console(_json({})) as (client, _):
        res = client.get("/static/js/api.js")

    assert res.status_code == 200
    assert "export const API" in res.text


@pytest.mark.parametrize(
    "browser_path, backend_path",
    [
        ("/api/events", "/phase1/events"),
        ("/api/sites", "/v1/sites"),
        ("/api/sites/s1", "/v1/sites/s1"),
        ("/api/sites/s1/vicinity", "/v1/sites/s1/vicinity"),
        ("/api/sites/s1/derived/slope", "/v1/sites/s1/derived/slope"),
        ("/api/sites/s1/bundle/terrain", "/v1/sites/s1/terrain"),
        ("/api/decisions", "/v1/decisions"),
        ("/api/fetch-log", "/v1/fetch-log"),
        ("/api/budget", "/v1/budget"),
        ("/api/replay/runs", "/v1/replay/runs"),
    ],
)
def test_every_get_route_maps_to_its_backend_path(browser_path, backend_path):
    with console(_json({})) as (client, calls):
        res = client.get(browser_path)

    assert res.status_code == 200, f"{browser_path} is not routed"
    assert calls[0].url.path == backend_path
