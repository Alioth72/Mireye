"""Derived optionality scoring.

The load-bearing test here is `test_no_power_means_zero_optionality`: it pins D11, the
decision that composition is multiplicative and not overridable. Under additive weights
flat unpowered farmland scores about half marks and Phase 3 alerts on ground that never
had the option -- the exact failure the brief calls "the keyword feed".
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from phase2.models import Datapoint
from phase2.mireye.licenses import license_for
from phase2.scoring import DEFAULT_WEIGHTS, METRICS, required_fields, score

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


def dp(field: str, value, *, status: str = "ok", source: str = "EIA_POWER") -> Datapoint:
    return Datapoint(
        site_id="s", field_name=field, value=value, status=status,
        source=source, license=license_for(source), confidence="high",
        fetched_at=NOW, ttl_seconds=86400,
    )


GOOD_SITE = [
    dp("nearest_transmission_line_voltage_kv", 230),
    dp("nearest_substation_distance_m", 1200),
    dp("fiber_broadband_available", True),
    dp("slope_degrees", 3.1),
    dp("within_floodplain_polygon", False),
    dp("intersects_wetland", False),
    dp("intersects_protected_area", False),
]


# --------------------------------------------------------------------------
# D11 -- the guard that separates this from a keyword feed
# --------------------------------------------------------------------------
def test_no_power_means_zero_optionality() -> None:
    """Flat, clear, fibered farmland with no transmission has NO data-centre
    optionality. Additive weighting would score it ~0.5 and trigger a false alert."""
    farmland = [
        dp("nearest_transmission_line_voltage_kv", None, status="absent"),
        dp("nearest_substation_distance_m", None, status="absent"),
        dp("fiber_broadband_available", True),
        dp("slope_degrees", 0.5),
        dp("within_floodplain_polygon", False),
        dp("intersects_wetland", False),
        dp("intersects_protected_area", False),
    ]
    result = score("data_center_optionality", farmland)

    assert result["score"] < 0.35, result
    assert result["components"]["terrain"]["score"] == 1.0  # perfectly flat
    assert result["components"]["fiber"]["score"] == 1.0    # fibered

    additive = sum(
        c["score"] * (c["weight"] or 0.0) for c in result["components"].values()
    )
    assert additive > result["score"], "additive scoring must be the more permissive one"


def test_protected_area_collapses_the_score() -> None:
    """A protected area is close to disqualifying no matter how good the power is."""
    protected = GOOD_SITE[:-1] + [dp("intersects_protected_area", True)]
    result = score("data_center_optionality", protected)
    # `clear` is a direct multiplier, not a weighted term -- so this really does collapse.
    assert result["score"] < 0.1, result
    assert result["components"]["clear"]["role"] == "penalty_multiplier"


def test_composition_is_always_reported() -> None:
    assert score("data_center_optionality", GOOD_SITE)["composition"] == "weighted_geometric_mean"


def test_override_cannot_change_composition() -> None:
    """A profile may reweight; it may not make the composition additive."""
    result = score(
        "data_center_optionality", GOOD_SITE, weights={"power": 0.9, "terrain": 0.1}
    )
    assert result["composition"] == "weighted_geometric_mean"


# --------------------------------------------------------------------------
# metric shapes
# --------------------------------------------------------------------------
def test_good_site_scores_well() -> None:
    result = score("data_center_optionality", GOOD_SITE)
    assert result["score"] > 0.85
    assert result["confidence"] == "high"
    assert result["fields_missing"] == []


def test_bess_ignores_fiber_entirely() -> None:
    """A battery does not need fiber; a data centre does. This is why Phase 1 must
    keep `subject` -- both are MORATORIUM-type events."""
    assert "fiber" not in DEFAULT_WEIGHTS["bess_optionality"]
    assert "fiber_broadband_available" not in required_fields("bess_optionality")
    assert "fiber_broadband_available" in required_fields("data_center_optionality")


def test_bess_score_unaffected_by_fiber() -> None:
    without_fiber = [d for d in GOOD_SITE if d.field_name != "fiber_broadband_available"]
    assert score("bess_optionality", GOOD_SITE)["score"] == pytest.approx(
        score("bess_optionality", without_fiber)["score"]
    )


@pytest.mark.parametrize("metric", METRICS)
def test_every_metric_runs_on_an_empty_site(metric: str) -> None:
    """No data must not crash -- it must produce a low-confidence answer."""
    result = score(metric, [])
    assert 0.0 <= result["score"] <= 1.0
    assert result["confidence"] == "low"
    assert result["fields_missing"]


@pytest.mark.parametrize("metric", METRICS)
def test_weights_are_normalised(metric: str) -> None:
    result = score(metric, GOOD_SITE)
    graded = [c["weight"] for c in result["components"].values() if c["weight"] is not None]
    assert sum(graded) == pytest.approx(1.0, abs=1e-3)


# --------------------------------------------------------------------------
# tri-state handling inside scoring
# --------------------------------------------------------------------------
def test_absent_constraint_is_evidence_not_a_gap() -> None:
    """`intersects_wetland: absent` means there is no wetland -- it should score the
    same as an explicit False, and must NOT appear in fields_missing."""
    absent = [
        dp("nearest_transmission_line_voltage_kv", 230),
        dp("nearest_substation_distance_m", 1200),
        dp("fiber_broadband_available", True),
        dp("slope_degrees", 3.1),
        dp("within_floodplain_polygon", None, status="absent"),
        dp("intersects_wetland", None, status="absent"),
        dp("intersects_protected_area", None, status="absent"),
    ]
    result = score("data_center_optionality", absent)
    assert result["components"]["clear"]["score"] == 1.0
    assert result["fields_missing"] == []


def test_missing_constraint_is_penalised_and_reported() -> None:
    """A field we never fetched is NOT the same as one the source said nothing about."""
    partial = [d for d in GOOD_SITE if d.field_name != "intersects_wetland"]
    result = score("data_center_optionality", partial)
    assert "intersects_wetland" in result["fields_missing"]
    assert result["components"]["clear"]["score"] < 1.0
    assert result["confidence"] in ("medium", "low")


def test_citations_carry_licence() -> None:
    osm = [dp("slope_degrees", 3.1, source="OpenInfraMap")]
    result = score("buildability", osm)
    slope_cite = [c for c in result["citations"] if c["field"] == "slope_degrees"][0]
    assert "ODbL" in slope_cite["license"]


def test_unknown_metric_names_the_known_ones() -> None:
    with pytest.raises(KeyError) as exc:
        score("does_this_moratorium_matter", GOOD_SITE)
    # There is deliberately no materiality metric here -- that is Phase 3's job.
    assert "data_center_optionality" in str(exc.value)
