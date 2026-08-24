"""Unit tests for the cheap, no-I/O gates that run before any Mireye credit is spent."""

from __future__ import annotations

from types import SimpleNamespace

from phase3.geography import geography_gate, haversine_km
from phase3.stage_policy import confidence_bucket, stage_gate


def _event(stage="ADOPTED", confidence=0.9, geography=None, jurisdiction="Seattle"):
    return {
        "canonical_id": "seattle:seattle_legistar:cb-1",
        "stage": stage,
        "confidence": confidence,
        "jurisdiction": jurisdiction,
        "geography": geography or {"type": "JURISDICTION", "name": jurisdiction},
    }


# --------------------------------------------------------------------------
# stage / confidence gate
# --------------------------------------------------------------------------
def test_proposed_is_not_alert_eligible():
    silence, reason = stage_gate(_event(stage="PROPOSED"))
    assert silence is True
    assert "PROPOSED" in reason


def test_heard_is_not_alert_eligible():
    silence, _ = stage_gate(_event(stage="HEARD"))
    assert silence is True


def test_withdrawn_and_tabled_are_not_alert_eligible():
    assert stage_gate(_event(stage="WITHDRAWN"))[0] is True
    assert stage_gate(_event(stage="TABLED"))[0] is True


def test_adopted_and_rejected_are_alert_eligible_when_confident():
    assert stage_gate(_event(stage="ADOPTED", confidence=0.9))[0] is False
    assert stage_gate(_event(stage="REJECTED", confidence=0.9))[0] is False


def test_low_confidence_adopted_event_is_silenced():
    """This is what keeps a keyword-only match (the heuristic fallback's fixed 0.4
    confidence, see monitor_records/ingest.py) from ever reaching ALERT."""
    silence, reason = stage_gate(_event(stage="ADOPTED", confidence=0.4))
    assert silence is True
    assert "confidence" in reason


def test_confidence_bucket_boundaries():
    assert confidence_bucket(0.4) == "low"
    assert confidence_bucket(0.59) == "low"
    assert confidence_bucket(0.6) == "medium"
    assert confidence_bucket(0.84) == "medium"
    assert confidence_bucket(0.85) == "high"
    assert confidence_bucket(0.99) == "high"


# --------------------------------------------------------------------------
# geography gate
# --------------------------------------------------------------------------
def _site(lat=47.6, lng=-122.3, locality="Seattle", region="Washington"):
    return SimpleNamespace(lat=lat, lng=lng, political_locality=locality, political_region=region)


def test_unresolved_geography_is_always_silenced():
    silence, reason = geography_gate(_event(geography={"type": "UNRESOLVED"}), _site())
    assert silence is True
    assert "UNRESOLVED" in reason or "unknown" in reason


def test_jurisdiction_match_passes():
    silence, _ = geography_gate(_event(geography={"type": "JURISDICTION", "name": "Seattle"}), _site())
    assert silence is False


def test_jurisdiction_mismatch_is_silenced():
    """An event decided in Bellevue must not alert a Seattle-registered site."""
    silence, reason = geography_gate(
        _event(geography={"type": "JURISDICTION", "name": "Bellevue"}), _site(locality="Seattle")
    )
    assert silence is True
    assert "Bellevue" in reason


def test_point_within_radius_passes():
    site = _site(lat=47.6062, lng=-122.3321)
    event = _event(geography={"type": "POINT", "latitude": 47.6065, "longitude": -122.3325})
    silence, _ = geography_gate(event, site)
    assert silence is False


def test_point_beyond_radius_is_silenced():
    site = _site(lat=47.6062, lng=-122.3321)
    # ~7km south -- well beyond the 1.5km default radius
    event = _event(geography={"type": "POINT", "latitude": 47.5480, "longitude": -122.3300})
    silence, reason = geography_gate(event, site)
    assert silence is True
    assert "km" in reason


def test_point_missing_coordinates_treated_as_unresolved():
    event = _event(geography={"type": "POINT"})
    silence, _ = geography_gate(event, _site())
    assert silence is True


def test_polygon_not_evaluated_treated_conservatively():
    event = _event(geography={"type": "POLYGON", "geojson": {}})
    silence, reason = geography_gate(event, _site())
    assert silence is True


def test_haversine_zero_distance():
    assert haversine_km(47.6, -122.3, 47.6, -122.3) == 0.0
