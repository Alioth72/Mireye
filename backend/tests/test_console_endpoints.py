"""The read-only console surface: GET /v1/sites, GET /v1/decisions, GET /v1/replay/runs.

These three routes exist so a frontend can render what the system already knows without
the act of rendering it changing anything -- no Mireye fetch, no credit, no new row. The
tests below therefore assert the *absence* of work as hard as they assert the payload:
`decide()` is monkeypatched to explode, the Mireye client dependency counts its own
construction, and the fetch log is checked for new entries after every read.

Both routers are mounted on a throwaway `FastAPI()` here rather than importing
`phase3.app`, because that module's lifespan calls the real `init_db()` for all three
phases and would create SQLite files on disk; the routers are the unit under test and
they are, by design, app-agnostic.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from phase2.config import Settings
from phase2.db import get_session as get_p2_session
from phase2.mireye.client import MireyeClient
from phase2.router import get_client, router as phase2_router
from phase3.db import get_session as get_p3_session
from phase3.models import P3Decision
from phase3.router import router as phase3_router
from tests.fakes.mireye_fake import BAD_SITE, GOOD_SITE, make_transport


@pytest.fixture()
def engine():
    """One in-memory engine for Phase 2 and Phase 3 alike -- SQLModel registers every
    table on one shared metadata anyway (see phase3/db.py's note), and sharing it keeps
    site_id references in p3_decision pointing at rows that really exist."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture()
def client_constructions():
    return []


@pytest.fixture()
def api(engine, client_constructions):
    app = FastAPI()
    app.include_router(phase2_router)
    app.include_router(phase3_router)

    def _get_session():
        with Session(engine) as session:
            yield session

    async def _get_client():
        # Recorded, not forbidden: POST /v1/sites legitimately needs a client. The read
        # endpoints must never appear in this list.
        client_constructions.append(1)
        settings = Settings(mireye_api_token="fake-token")
        c = MireyeClient(settings=settings, transport=make_transport())
        async with c:
            yield c

    app.dependency_overrides[get_p2_session] = _get_session
    app.dependency_overrides[get_p3_session] = _get_session
    app.dependency_overrides[get_client] = _get_client
    with TestClient(app) as tc:
        yield tc
    app.dependency_overrides.clear()


@pytest.fixture()
def no_pipeline(monkeypatch):
    """Any read endpoint that reaches `decide()` is a bug, not a slow path."""

    async def _boom(*args, **kwargs):
        raise AssertionError("a read endpoint invoked the Phase 3 pipeline")

    monkeypatch.setattr("phase3.router.decide", _boom)


def _register(api, coords, label):
    resp = api.post("/v1/sites", json={"label": label, "lat": coords[0], "lng": coords[1]})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _store_decision(engine, **kw):
    defaults = dict(
        canonical_id="seattle:seattle_legistar:CB1",
        stage="ADOPTED",
        confidence_bucket="high",
        site_id="site-1",
        decision="ALERT",
        reasons=["because"],
        metric="data_center_optionality",
        score=0.8,
        components={"power": {"score": 1.0, "weight": 0.4, "basis": "230kV at 0.4km"}},
        government_evidence=[{"document_id": "d1"}],
        decided_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    with Session(engine) as session:
        row = P3Decision(**defaults)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


# --------------------------------------------------------------------------
# GET /v1/sites
# --------------------------------------------------------------------------
def test_list_sites_is_empty_before_any_registration(api):
    resp = api.get("/v1/sites")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_list_sites_returns_every_registered_site_with_the_console_keys(api):
    good = _register(api, GOOD_SITE, "good site")
    bad = _register(api, BAD_SITE, "bad site")

    resp = api.get("/v1/sites")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {row["id"] for row in body} == {good, bad}

    row = next(r for r in body if r["id"] == good)
    for key in (
        "id",
        "label",
        "lat",
        "lng",
        "address_raw",
        "political_region",
        "political_county",
        "political_locality",
        "degraded",
    ):
        assert key in row, key
    assert row["label"] == "good site"
    assert row["lat"] == GOOD_SITE[0]
    assert row["political_locality"] == "Seattle"
    assert row["political_county"] == "King County"
    assert row["degraded"] is False


def test_list_sites_matches_the_single_site_payload_exactly(api):
    """One helper serves both routes; a list row and a detail row must not drift."""
    site_id = _register(api, GOOD_SITE, "good site")
    listed = next(r for r in api.get("/v1/sites").json() if r["id"] == site_id)
    detail = api.get("/v1/sites/" + site_id).json()
    assert listed == detail


def test_list_sites_spends_nothing(api):
    _register(api, GOOD_SITE, "good site")
    before = len(api.get("/v1/fetch-log").json())
    assert api.get("/v1/sites").status_code == 200
    assert len(api.get("/v1/fetch-log").json()) == before


# --------------------------------------------------------------------------
# GET /v1/decisions
# --------------------------------------------------------------------------
def test_list_decisions_is_empty_before_anything_is_decided(api, no_pipeline):
    resp = api.get("/v1/decisions")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_list_decisions_returns_stored_rows_newest_first(api, engine, no_pipeline):
    _store_decision(engine, canonical_id="cid-old", decided_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
    _store_decision(engine, canonical_id="cid-new", decided_at=datetime(2026, 8, 20, tzinfo=timezone.utc))

    body = api.get("/v1/decisions").json()
    assert [row["canonical_id"] for row in body] == ["cid-new", "cid-old"]

    row = body[0]
    assert row["decision"] == "ALERT"
    assert row["stage"] == "ADOPTED"
    assert row["site_id"] == "site-1"
    assert row["reasons"] == ["because"]
    assert row["government_evidence"] == [{"document_id": "d1"}]
    assert row["metric"] == "data_center_optionality"
    assert row["score"] == 0.8
    # `components` on the stored row surfaces as `physical_components` on the contract
    assert row["physical_components"]["power"]["basis"] == "230kV at 0.4km"
    assert row["replayed"] is True
    assert row["evaluated_at"].startswith("2026-08-20")
    # p3_decision keys on canonical_id, so there is no stored event_id to hand back
    assert row["event_id"] is None


def test_list_decisions_filters_by_canonical_id(api, engine, no_pipeline):
    _store_decision(engine, canonical_id="cid-a")
    _store_decision(engine, canonical_id="cid-b")

    body = api.get("/v1/decisions", params={"canonical_id": "cid-a"}).json()
    assert [row["canonical_id"] for row in body] == ["cid-a"]


def test_list_decisions_filters_by_site_id(api, engine, no_pipeline):
    _store_decision(engine, canonical_id="cid-a", site_id="site-1")
    _store_decision(engine, canonical_id="cid-a", site_id="site-2")

    body = api.get("/v1/decisions", params={"site_id": "site-2"}).json()
    assert [row["site_id"] for row in body] == ["site-2"]


def test_list_decisions_filters_by_decision_value(api, engine, no_pipeline):
    _store_decision(engine, canonical_id="cid-a", decision="ALERT")
    _store_decision(engine, canonical_id="cid-b", decision="SILENCE")

    body = api.get("/v1/decisions", params={"decision": "SILENCE"}).json()
    assert [row["canonical_id"] for row in body] == ["cid-b"]


def test_list_decisions_honours_limit_and_caps_it(api, engine, no_pipeline):
    for i in range(5):
        _store_decision(
            engine,
            canonical_id="cid-" + str(i),
            decided_at=datetime(2026, 8, 1 + i, tzinfo=timezone.utc),
        )

    body = api.get("/v1/decisions", params={"limit": 2}).json()
    assert [row["canonical_id"] for row in body] == ["cid-4", "cid-3"]

    assert len(api.get("/v1/decisions").json()) == 5  # the default 200 covers everything
    assert api.get("/v1/decisions", params={"limit": 1001}).status_code == 422
    assert api.get("/v1/decisions", params={"limit": 0}).status_code == 422


def test_list_decisions_never_invokes_the_pipeline_or_spends_credits(
    api, engine, no_pipeline, client_constructions
):
    """The whole point of the endpoint: reading the decision history must not decide
    anything. `no_pipeline` makes any call to decide() an outright failure; the client
    counter and the fetch log prove no Mireye traffic happened either."""
    _store_decision(engine, canonical_id="cid-a")
    _register(api, GOOD_SITE, "good site")
    clients_before = len(client_constructions)
    log_before = len(api.get("/v1/fetch-log").json())

    for params in ({}, {"canonical_id": "cid-a"}, {"site_id": "site-1"}, {"limit": 10}):
        assert api.get("/v1/decisions", params=params).status_code == 200

    assert len(client_constructions) == clients_before
    assert len(api.get("/v1/fetch-log").json()) == log_before
    with Session(engine) as session:
        assert len(session.exec(select(P3Decision)).all()) == 1  # reading wrote nothing


# --------------------------------------------------------------------------
# GET /v1/replay/runs
# --------------------------------------------------------------------------
def test_replay_runs_reports_an_honest_empty_state(api, no_pipeline):
    resp = api.get("/v1/replay/runs")
    assert resp.status_code == 200, resp.text  # empty state, not an error
    body = resp.json()

    assert body["corpus"] is None
    assert body["total_decisions"] == 0
    assert body["by_decision"] == {}
    assert body["by_stage"] == {}
    assert body["distinct_canonical_ids"] == 0

    note = body["note"].lower()
    assert "corpus" in note
    assert "lead time" in note
    assert "adoption" in note
    assert "press" in note


def test_replay_runs_fabricates_no_precision_or_recall(api, engine, no_pipeline):
    _store_decision(engine, canonical_id="cid-a", decision="ALERT")
    _store_decision(engine, canonical_id="cid-b", decision="SILENCE")

    body = api.get("/v1/replay/runs").json()
    for invented in (
        "precision",
        "recall",
        "f1",
        "false_positive_rate",
        "lead_time_days",
        "days_before_adoption",
        "days_before_press",
    ):
        assert invented not in body, invented
    assert body["corpus"] is None


def test_replay_runs_counts_only_what_the_stored_decisions_support(api, engine, no_pipeline):
    _store_decision(engine, canonical_id="cid-a", stage="ADOPTED", decision="ALERT")
    _store_decision(engine, canonical_id="cid-a", site_id="site-2", stage="ADOPTED", decision="SILENCE")
    _store_decision(engine, canonical_id="cid-b", stage="PROPOSED", decision="SILENCE")

    body = api.get("/v1/replay/runs").json()
    assert body["total_decisions"] == 3
    assert body["by_decision"] == {"ALERT": 1, "SILENCE": 2}
    assert body["by_stage"] == {"ADOPTED": 2, "PROPOSED": 1}
    assert body["distinct_canonical_ids"] == 2


def test_replay_runs_spends_nothing(api, engine, no_pipeline, client_constructions):
    _register(api, GOOD_SITE, "good site")
    clients_before = len(client_constructions)
    log_before = len(api.get("/v1/fetch-log").json())

    assert api.get("/v1/replay/runs").status_code == 200

    assert len(client_constructions) == clients_before
    assert len(api.get("/v1/fetch-log").json()) == log_before
