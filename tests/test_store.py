"""The datapoint store: tri-state status, TTL freshness, and the never-cache-a-failure rule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from phase2.config import Settings
from phase2.models import Datapoint, Site
from phase2.mireye.schemas import FieldRecord
from phase2.store import (
    is_answer,
    is_stale,
    needs_fetch,
    read_fields,
    serialize,
    upsert_records,
)

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Site(id="site-1", lat=47.6062, lng=-122.3321, label="test"))
        s.commit()
        yield s


@pytest.fixture
def settings():
    return Settings(phase2_failed_negative_cache_s=300)


def ok(value, **kw) -> FieldRecord:
    return FieldRecord(
        value=value,
        status="ok",
        source=kw.pop("source", "EIA Energy Atlas"),
        fetched_at=kw.pop("fetched_at", NOW),
        ttl_seconds=kw.pop("ttl_seconds", 86400),
        confidence=kw.pop("confidence", "high"),
        **kw,
    )


# --------------------------------------------------------------------------
# `absent` is a real answer
# --------------------------------------------------------------------------
def test_absent_is_an_answer_not_a_missing_field(session, settings) -> None:
    """`intersects_wetland: absent` means there is no wetland here. That is evidence
    which RAISES optionality -- reporting it as missing would invert a decision."""
    upsert_records(
        session,
        "site-1",
        {"intersects_wetland": FieldRecord(value=None, status="absent", source="USGS NHD",
                                           fetched_at=NOW, ttl_seconds=86400)},
        now=NOW,
    )
    read = read_fields(session, "site-1", ["intersects_wetland"], now=NOW, settings=settings)

    assert [dp.field_name for dp in read.answers] == ["intersects_wetland"]
    assert read.to_fetch == []
    assert is_answer(read.answers[0])


def test_absent_is_served_not_refetched(session, settings) -> None:
    upsert_records(
        session,
        "site-1",
        {"wetland_acres": FieldRecord(value=None, status="absent", source="USGS NHD",
                                      fetched_at=NOW, ttl_seconds=86400)},
        now=NOW,
    )
    read = read_fields(session, "site-1", ["wetland_acres"], now=NOW + timedelta(hours=1),
                       settings=settings)
    assert read.to_fetch == []


# --------------------------------------------------------------------------
# never cache a failure as an answer
# --------------------------------------------------------------------------
def test_failed_is_never_stored_as_a_value(session, settings) -> None:
    """HTTP is 200 on a failed field, so a naive upsert would freeze the failure as
    truth -- and a null `intersects_wetland` reads as 'no wetland'."""
    upsert_records(
        session,
        "site-1",
        {"slope_degrees": FieldRecord(value=None, status="failed", error="upstream timeout",
                                      retryable=True)},
        now=NOW,
    )
    dp = session.exec(select(Datapoint).where(Datapoint.field_name == "slope_degrees")).one()

    assert dp.status == "failed"
    assert dp.value is None
    assert not is_answer(dp)

    read = read_fields(session, "site-1", ["slope_degrees"], now=NOW, settings=settings)
    assert read.answers == []
    assert [dp.field_name for dp in read.withheld] == ["slope_degrees"]


def test_failed_does_not_clobber_a_previously_good_value(session, settings) -> None:
    upsert_records(session, "site-1", {"slope_degrees": ok(3.1)}, now=NOW)
    upsert_records(
        session,
        "site-1",
        {"slope_degrees": FieldRecord(value=None, status="failed", error="boom", retryable=True)},
        now=NOW + timedelta(minutes=1),
    )
    dp = session.exec(select(Datapoint).where(Datapoint.field_name == "slope_degrees")).one()

    assert dp.status == "ok"
    assert dp.value == 3.1
    assert "refresh failed" in (dp.notes or "")


def test_failure_is_negatively_cached_then_retried(session, settings) -> None:
    """We must not re-fetch a broken field on every single request, but we must not
    give up on it either."""
    upsert_records(
        session,
        "site-1",
        {"fiber_broadband_available": FieldRecord(value=None, status="failed",
                                                  error="timeout", retryable=True)},
        now=NOW,
    )
    inside = read_fields(session, "site-1", ["fiber_broadband_available"],
                         now=NOW + timedelta(seconds=60), settings=settings)
    assert inside.to_fetch == []

    outside = read_fields(session, "site-1", ["fiber_broadband_available"],
                          now=NOW + timedelta(seconds=600), settings=settings)
    assert outside.to_fetch == ["fiber_broadband_available"]


def test_non_retryable_failures_back_off_much_harder(session, settings) -> None:
    """`retryable: false` is a structured upstream refusal -- retrying will not help."""
    upsert_records(
        session,
        "site-1",
        {"grading_difficulty_class": FieldRecord(value=None, status="failed",
                                                 error="not entitled", retryable=False)},
        now=NOW,
    )
    read = read_fields(session, "site-1", ["grading_difficulty_class"],
                       now=NOW + timedelta(seconds=600), settings=settings)
    assert read.to_fetch == []


# --------------------------------------------------------------------------
# TTL freshness
# --------------------------------------------------------------------------
def test_fresh_value_is_not_refetched(session, settings) -> None:
    upsert_records(session, "site-1", {"elevation": ok(56.0, ttl_seconds=31_536_000)}, now=NOW)
    read = read_fields(session, "site-1", ["elevation"], now=NOW + timedelta(days=30),
                       settings=settings)
    assert read.to_fetch == []
    assert not is_stale(read.answers[0], now=NOW + timedelta(days=30))


def test_stale_value_is_refetched(session, settings) -> None:
    """EIA Atlas carries a 1-day TTL; transmission really can move under us."""
    upsert_records(
        session,
        "site-1",
        {"nearest_transmission_line_voltage_kv": ok(230, ttl_seconds=86400)},
        now=NOW,
    )
    read = read_fields(session, "site-1", ["nearest_transmission_line_voltage_kv"],
                       now=NOW + timedelta(days=2), settings=settings)
    assert read.to_fetch == ["nearest_transmission_line_voltage_kv"]


def test_missing_field_is_fetched(session, settings) -> None:
    read = read_fields(session, "site-1", ["slope_degrees"], now=NOW, settings=settings)
    assert read.to_fetch == ["slope_degrees"]
    assert read.answers == []


def test_needs_fetch_on_absent_row() -> None:
    assert needs_fetch(None) is True


def test_read_splits_a_mixed_bundle(session, settings) -> None:
    upsert_records(
        session,
        "site-1",
        {
            "slope_degrees": ok(3.1),
            "elevation": ok(56.0, ttl_seconds=31_536_000),
        },
        now=NOW,
    )
    read = read_fields(
        session,
        "site-1",
        ["slope_degrees", "elevation", "grading_difficulty_class"],
        now=NOW,
        settings=settings,
    )
    assert {dp.field_name for dp in read.answers} == {"slope_degrees", "elevation"}
    assert read.to_fetch == ["grading_difficulty_class"]


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------
def test_licence_is_resolved_from_runtime_source(session, settings) -> None:
    """OSM-sourced grid fields are ODbL; our derived scores inherit share-alike, so the
    licence has to be captured at write time from the RUNTIME source."""
    upsert_records(
        session,
        "site-1",
        {
            "nearest_osm_transmission_line_voltage_kv": ok(115, source="OpenInfraMap"),
            "nearest_transmission_line_voltage_kv": ok(230, source="EIA Energy Atlas"),
        },
        now=NOW,
    )
    rows = {dp.field_name: dp for dp in session.exec(select(Datapoint)).all()}
    assert "ODbL" in rows["nearest_osm_transmission_line_voltage_kv"].license
    assert "public domain" in rows["nearest_transmission_line_voltage_kv"].license.lower()


def test_unknown_source_gets_no_licence_guess(session, settings) -> None:
    upsert_records(session, "site-1", {"slope_degrees": ok(3.1, source="Somebody's Blog")},
                   now=NOW)
    dp = session.exec(select(Datapoint).where(Datapoint.field_name == "slope_degrees")).one()
    assert dp.license is None


def test_serialize_keeps_full_provenance(session, settings) -> None:
    upsert_records(session, "site-1", {"slope_degrees": ok(3.1)}, now=NOW)
    dp = session.exec(select(Datapoint).where(Datapoint.field_name == "slope_degrees")).one()
    payload = serialize(dp, now=NOW)

    for key in ("field", "value", "status", "source", "source_url", "license",
                "confidence", "fetched_at", "ttl_seconds", "stale"):
        assert key in payload
    assert payload["value"] == 3.1
    assert payload["stale"] is False


def test_serialize_exposes_error_detail_for_failures(session, settings) -> None:
    upsert_records(
        session,
        "site-1",
        {"slope_degrees": FieldRecord(value=None, status="failed", error="timeout",
                                      retryable=True)},
        now=NOW,
    )
    dp = session.exec(select(Datapoint).where(Datapoint.field_name == "slope_degrees")).one()
    payload = serialize(dp, now=NOW)
    assert payload["error"] == "timeout"
    assert payload["retryable"] is True
