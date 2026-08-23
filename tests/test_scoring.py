"""Derived optionality scoring.

Two load-bearing groups here:

* **Composition** — `test_no_power_means_zero_optionality` pins the multiplicative rule.
  Under additive weights, flat unpowered farmland scores about half marks and Phase 3
  alerts on ground that never had the option.
* **Calibration regressions** — every threshold in `scoring.py` is now sourced from
  Mireye's own `interpretation_hints`. Two invented bands produced real errors before
  that; both have a named regression test below.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from phase2.models import Datapoint
from phase2.mireye.licenses import license_for
from phase2.scoring import (
    DEFAULT_WEIGHTS,
    METRICS,
    all_feature_fields,
    feature_row,
    required_fields,
    score,
)

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def dp(field: str, value, *, status: str = "ok", source: str = "EIA_POWER") -> Datapoint:
    return Datapoint(
        site_id="s", field_name=field, value=value, status=status,
        source=source, license=license_for(source), confidence="high",
        fetched_at=NOW, ttl_seconds=86400,
    )


def site(**over) -> list:
    """A strong site, with named fields overridden."""
    base = {
        "nearest_transmission_line_voltage_kv": 230,
        "max_transmission_line_voltage_kv_within_radius": 230,
        "nearest_transmission_line_voltage_class": "230-345",
        "nearest_substation_distance_m": 1200,
        "fiber_broadband_available": True,
        "fiber_provider_count": 3,
        "nearest_long_haul_rail_corridor_distance_m": 800,
        "slope_degrees": 3.1,
        "design_wet_bulb_temperature_0_4pct_degc": 17.0,
        "days_above_32c_annual_count": 4,
        "within_water_service_area": True,
        "surface_water_supply_use_index_huc12": 0.05,
        "avg_retail_electricity_price_industrial_usd_per_kwh": 0.055,
        "nearest_major_road_distance_m": 700,
        "intersects_protected_area": False,
        "protected_area_gap_status": None,
        "protected_area_name": None,
        "within_floodplain_polygon": False,
        "intersects_wetland": False,
        "in_air_quality_nonattainment": False,
    }
    base.update(over)
    return [dp(k, v, status="absent" if v is None else "ok") for k, v in base.items()]


GOOD = site()


# ==========================================================================
# calibration regressions — the two bugs found against Mireye's own hints
# ==========================================================================
def test_gap4_parkland_is_not_a_disqualifier() -> None:
    """REGRESSION. A municipal golf course (PAD-US GAP 4, designation LP, manager SPR)
    scored 0.046 — treated as hard-disqualified. Mireye is explicit: GAP "4 ~ nominal —
    no mandate to prevent conversion (city parks, military). Do NOT overstate GAP 4 as a
    development constraint."

    This was a FALSE QUIET: the landowner would have received no alert at all.
    """
    parkland = site(intersects_protected_area=True, protected_area_gap_status=4)
    result = score("data_center_optionality", parkland)

    assert result["components"]["clear"]["score"] >= 0.9, result["components"]["clear"]
    assert result["score"] > 0.75, result["score"]


def test_gap1_wilderness_still_collapses_the_score() -> None:
    """The fix must not swing the other way — GAP 1/2 is real conservation protection."""
    wilderness = site(intersects_protected_area=True, protected_area_gap_status=1)
    assert score("data_center_optionality", wilderness)["score"] < 0.35


@pytest.mark.parametrize("gap,lo,hi", [(1, 0.0, 0.1), (2, 0.1, 0.3), (3, 0.5, 0.8), (4, 0.9, 1.0)])
def test_gap_status_grades_monotonically(gap: int, lo: float, hi: float) -> None:
    r = score("data_center_optionality",
              site(intersects_protected_area=True, protected_area_gap_status=gap))
    assert lo <= r["components"]["clear"]["score"] <= hi


def test_ordinary_urban_slope_is_not_penalised_as_unbuildable() -> None:
    """REGRESSION. Slope was penalised from 10 degrees, scoring downtown Seattle's 12.6
    at 0.35. Mireye: "Slope >25 deg complicates conventional construction." A 12.6-degree
    hillside is ordinary urban ground, not a construction problem."""
    r = score("data_center_optionality", site(slope_degrees=12.6))
    assert r["components"]["terrain"]["score"] >= 0.8


def test_genuinely_steep_ground_is_still_penalised() -> None:
    assert score("data_center_optionality",
                 site(slope_degrees=30))["components"]["terrain"]["score"] <= 0.3


def test_power_reads_the_better_of_nearest_and_max_within_radius() -> None:
    """Mireye: nearest voltage "may be LOWER than max_transmission_line_voltage_kv_within
    _radius if a higher-voltage line runs slightly farther; read both"."""
    weak_nearest = site(nearest_transmission_line_voltage_kv=69,
                        max_transmission_line_voltage_kv_within_radius=500)
    r = score("data_center_optionality", weak_nearest)
    assert r["components"]["power"]["score"] >= 0.9
    assert "500" in r["components"]["power"]["basis"]


def test_absent_voltage_with_a_voltage_class_is_not_treated_as_no_line() -> None:
    """Mireye: "Null here != 'no voltage' — check nearest_transmission_line_voltage_class"."""
    unpublished = site(nearest_transmission_line_voltage_kv=None,
                       max_transmission_line_voltage_kv_within_radius=None,
                       nearest_transmission_line_voltage_class="100-161")
    r = score("data_center_optionality", unpublished)
    assert r["components"]["power"]["score"] > 0.3
    assert "voltage class" in r["components"]["power"]["basis"]


def test_genuinely_absent_transmission_says_so_precisely() -> None:
    """Both Seattle bluffs return absent across voltage, class and distance. The basis
    must claim "within search radius" — not that no line exists anywhere."""
    none_in_range = site(nearest_transmission_line_voltage_kv=None,
                         max_transmission_line_voltage_kv_within_radius=None,
                         nearest_transmission_line_voltage_class=None)
    r = score("data_center_optionality", none_in_range)
    assert r["components"]["power"]["score"] <= 0.1
    assert "search radius" in r["components"]["power"]["basis"]


def test_fiber_alone_no_longer_carries_a_quarter_of_the_score() -> None:
    """Mireye: fiber_broadband_available is mass-market FTTP, "an availability FLOOR,
    NOT an interconnect-ecosystem signal". It was True at all ten test sites and so
    discriminated nothing while holding weight 0.25."""
    assert "fiber" not in DEFAULT_WEIGHTS["data_center_optionality"]
    assert "interconnect" in DEFAULT_WEIGHTS["data_center_optionality"]
    assert "nearest_long_haul_rail_corridor_distance_m" in required_fields(
        "data_center_optionality")


def test_nonattainment_is_friction_not_a_ban() -> None:
    """Nonattainment "triggers stricter New Source Review / emission-offset permitting
    for combustion equipment" — it raises cost and timeline for backup generation. Real,
    but not disqualifying."""
    naa = score("data_center_optionality", site(in_air_quality_nonattainment=True))
    clean = score("data_center_optionality", GOOD)
    assert naa["score"] < clean["score"]
    assert naa["score"] > 0.5


def test_hot_humid_climate_hurts_evaporative_cooling() -> None:
    """Mireye calls design wet bulb "THE variable for evaporative-cooling viability"."""
    phoenix = score("data_center_optionality",
                    site(design_wet_bulb_temperature_0_4pct_degc=26.0,
                         days_above_32c_annual_count=170))
    seattle = score("data_center_optionality", GOOD)
    assert phoenix["components"]["cooling"]["score"] < 0.4
    assert phoenix["score"] < seattle["score"]


def test_water_stress_reduces_the_score() -> None:
    """High SUI "flags watersheds where consumptive use approaches supply"."""
    stressed = score("data_center_optionality",
                     site(surface_water_supply_use_index_huc12=0.95))
    assert stressed["components"]["water"]["score"] < 0.6


# ==========================================================================
# composition — the guard that separates this from a keyword feed
# ==========================================================================
def test_no_power_means_zero_optionality() -> None:
    farmland = site(nearest_transmission_line_voltage_kv=None,
                    max_transmission_line_voltage_kv_within_radius=None,
                    nearest_transmission_line_voltage_class=None,
                    nearest_substation_distance_m=None,
                    slope_degrees=0.5)
    r = score("data_center_optionality", farmland)

    assert r["score"] < 0.35, r
    assert r["components"]["terrain"]["score"] == 1.0
    additive = sum(c["score"] * (c["weight"] or 0.0) for c in r["components"].values())
    assert additive > r["score"], "additive scoring must be the more permissive one"


def test_composition_and_calibration_status_are_always_reported() -> None:
    r = score("data_center_optionality", GOOD)
    assert r["composition"] == "weighted_geometric_mean"
    assert "provisional" in r["calibration"]


def test_override_cannot_change_composition() -> None:
    r = score("data_center_optionality", GOOD, weights={"power": 0.9, "terrain": 0.1})
    assert r["composition"] == "weighted_geometric_mean"


# ==========================================================================
# metric shapes
# ==========================================================================
def test_good_site_scores_well() -> None:
    r = score("data_center_optionality", GOOD)
    assert r["score"] > 0.85
    assert r["confidence"] == "high"
    assert r["fields_missing"] == []


def test_bess_ignores_cooling_fibre_and_water() -> None:
    """A battery needs grid and ground. It does not need fibre, cooling water, or
    evaporative-cooling climate — which is why Phase 1 must keep `subject`."""
    w = DEFAULT_WEIGHTS["bess_optionality"]
    assert "interconnect" not in w and "cooling" not in w and "water" not in w
    assert "fiber_broadband_available" not in required_fields("bess_optionality")
    assert "fiber_broadband_available" in required_fields("data_center_optionality")


@pytest.mark.parametrize("metric", METRICS)
def test_every_metric_runs_on_an_empty_site(metric: str) -> None:
    r = score(metric, [])
    assert 0.0 <= r["score"] <= 1.0
    assert r["confidence"] == "low"
    assert r["fields_missing"]


@pytest.mark.parametrize("metric", METRICS)
def test_weights_are_normalised(metric: str) -> None:
    graded = [c["weight"] for c in score(metric, GOOD)["components"].values()
              if c["weight"] is not None]
    assert sum(graded) == pytest.approx(1.0, abs=1e-3)


# ==========================================================================
# tri-state handling
# ==========================================================================
def test_absent_constraint_is_evidence_not_a_gap() -> None:
    """`intersects_wetland: absent` means there is no wetland — same as an explicit
    False, and it must NOT be reported as missing."""
    r = score("data_center_optionality",
              site(within_floodplain_polygon=None, intersects_wetland=None,
                   intersects_protected_area=None))
    assert r["components"]["clear"]["score"] == 1.0
    assert r["fields_missing"] == []


def test_missing_constraint_is_penalised_and_reported() -> None:
    partial = [d for d in GOOD if d.field_name not in
               ("intersects_protected_area", "protected_area_gap_status")]
    r = score("data_center_optionality", partial)
    assert "intersects_protected_area" in r["fields_missing"]
    assert r["components"]["clear"]["score"] < 1.0


def test_citations_carry_licence() -> None:
    osm = [dp("slope_degrees", 3.1, source="OpenInfraMap")]
    r = score("buildability", osm)
    cite = [c for c in r["citations"] if c["field"] == "slope_degrees"][0]
    assert "ODbL" in cite["license"]


def test_unknown_metric_names_the_known_ones() -> None:
    with pytest.raises(KeyError) as exc:
        score("does_this_moratorium_matter", GOOD)
    assert "data_center_optionality" in str(exc.value)


# ==========================================================================
# calibration export
# ==========================================================================
def test_feature_row_carries_raw_values_and_status() -> None:
    """The export feeding weight calibration must include RAW values, not just
    sub-scores — the fitting has to be able to bypass our bands entirely if the
    evidence says they are wrong."""
    row = feature_row("test-site", GOOD, extra={"is_known_datacenter": 1})
    assert row["site"] == "test-site"
    assert row["is_known_datacenter"] == 1
    assert row["slope_degrees"] == 3.1
    assert row["slope_degrees__status"] == "ok"
    assert row["cmp_power"] > 0.9
    assert row["score_data_center_optionality"] > 0.85


def test_every_scored_field_is_exportable() -> None:
    exported = set(all_feature_fields())
    for metric in METRICS:
        assert set(required_fields(metric)) <= exported, metric
