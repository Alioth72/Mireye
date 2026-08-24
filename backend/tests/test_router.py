"""The Phase 2 HTTP surface added on top of the already-tested store/bundles/client:
site registration, the cache-miss autofetch orchestrator, and the fetch-log audit trail.
Exercised against the fake Mireye transport (tests/fakes/mireye_fake.py) via a FastAPI
dependency override -- no real network or MIREYE_API_TOKEN needed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from phase2.app import app
from phase2.config import Settings
from phase2.db import get_session
from phase2.mireye.client import MireyeClient
from phase2.router import get_client
from tests.fakes.mireye_fake import BAD_SITE, GOOD_SITE, make_transport


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture()
def client(engine):
    def _get_session():
        with Session(engine) as session:
            yield session

    async def _get_client():
        settings = Settings(mireye_api_token="fake-token")
        c = MireyeClient(settings=settings, transport=make_transport())
        async with c:
            yield c

    app.dependency_overrides[get_session] = _get_session
    app.dependency_overrides[get_client] = _get_client
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()


def test_register_site_by_latlng(client):
    resp = client.post("/v1/sites", json={"label": "good site", "lat": GOOD_SITE[0], "lng": GOOD_SITE[1]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lat"] == GOOD_SITE[0]
    # boundaries bundle pulled once at registration
    assert body["political_locality"] == "Seattle"
    assert body["political_county"] == "King County"


def test_register_site_by_address_geocodes_once(client):
    resp = client.post("/v1/sites", json={"address": "1000 1st Ave S, Seattle, WA"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lat"] == GOOD_SITE[0]
    assert body["geocode_provider"] == "fake-geocoder"
    assert body["degraded"] is False


def test_register_site_rejects_both_address_and_latlng(client):
    resp = client.post("/v1/sites", json={"address": "x", "lat": 1.0, "lng": 2.0})
    assert resp.status_code == 400


def test_bundle_fetch_autofetches_on_cache_miss_then_hits_cache(client):
    site_id = client.post("/v1/sites", json={"lat": GOOD_SITE[0], "lng": GOOD_SITE[1]}).json()["id"]

    first = client.get(f"/v1/sites/{site_id}/grid")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["cache"]["fetched"] == 6  # all 6 grid fields were a cache miss
    assert body["cache"]["hits"] == 0
    assert len(body["datapoints"]) == 6
    voltage = next(dp for dp in body["datapoints"] if dp["field"] == "nearest_transmission_line_voltage_kv")
    assert voltage["value"] == 230
    assert voltage["status"] == "ok"

    second = client.get(f"/v1/sites/{site_id}/grid")
    body2 = second.json()
    assert body2["cache"]["hits"] == 6  # now served from the TTL cache, 0 new fetches
    assert body2["cache"]["fetched"] == 0


def test_bundle_fetch_preserves_absent_as_an_answer(client):
    site_id = client.post("/v1/sites", json={"lat": GOOD_SITE[0], "lng": GOOD_SITE[1]}).json()["id"]
    body = client.get(f"/v1/sites/{site_id}/water").json()
    wetland = next(dp for dp in body["datapoints"] if dp["field"] == "intersects_wetland")
    assert wetland["status"] == "absent"
    assert wetland["value"] is None


def test_unknown_bundle_rejected(client):
    site_id = client.post("/v1/sites", json={"lat": GOOD_SITE[0], "lng": GOOD_SITE[1]}).json()["id"]
    resp = client.get(f"/v1/sites/{site_id}/data_center_moratorium")
    assert resp.status_code == 400


def test_fetch_log_records_every_credit_spend(client):
    site_id = client.post("/v1/sites", json={"lat": BAD_SITE[0], "lng": BAD_SITE[1]}).json()["id"]
    client.get(f"/v1/sites/{site_id}/terrain")

    log = client.get("/v1/fetch-log", params={"site_id": site_id}).json()
    triggers = [row["trigger"] for row in log]
    assert "registration" in triggers  # the boundaries pull at POST /v1/sites
    assert "cache_miss" in triggers  # the terrain bundle fetch
    for row in log:
        assert row["ok"] is True
        assert row["fields"]


def test_two_sites_share_no_state(client):
    good_id = client.post("/v1/sites", json={"lat": GOOD_SITE[0], "lng": GOOD_SITE[1]}).json()["id"]
    bad_id = client.post("/v1/sites", json={"lat": BAD_SITE[0], "lng": BAD_SITE[1]}).json()["id"]

    good = client.get(f"/v1/sites/{good_id}/grid").json()
    bad = client.get(f"/v1/sites/{bad_id}/grid").json()

    good_v = next(dp["value"] for dp in good["datapoints"] if dp["field"] == "nearest_transmission_line_voltage_kv")
    bad_v = next(dp["value"] for dp in bad["datapoints"] if dp["field"] == "nearest_transmission_line_voltage_kv")
    assert good_v == 230
    assert bad_v == 12
