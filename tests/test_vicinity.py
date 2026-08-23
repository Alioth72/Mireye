"""Vicinity sampling — geometry, per-class aggregation, and the West Seattle regression.

The acceptance test for this whole module is `test_west_seattle_transmission_is_found`.
Point sampling reported that site as having no transmission, and it was published as a
`quiet` case. A ring around the same coordinate finds 230 kV. Point sampling is
asymmetric — it can only under-report proximity — so it manufactures false quiets, which
is the worst error this product can make: the landowner is never warned.
"""

from __future__ import annotations

import math

import pytest

from phase2.vicinity import (
    DEFAULT_RINGS,
    MAX_BATCH_LOCATIONS,
    Observation,
    aggregate,
    classify,
    policy_for,
    ring_points,
    summarise,
)


# ==========================================================================
# geometry
# ==========================================================================
def test_default_ring_is_exactly_one_batch_call() -> None:
    """Centroid + 8 bearings x 3 rings = 25, the hard batch cap. Adding a ring means
    two calls and breaks the one-call-per-vicinity property."""
    pts = ring_points(47.5707, -122.3870)
    assert len(pts) == MAX_BATCH_LOCATIONS
    assert sum(1 for p in pts if p.is_centroid) == 1


def test_ring_exceeding_the_cap_is_refused() -> None:
    with pytest.raises(ValueError, match="25-location batch cap"):
        ring_points(47.6, -122.3, rings=(200, 400, 600, 800))


def test_longitude_is_latitude_corrected() -> None:
    """At Seattle's 47.6 degrees a degree of longitude is only ~67% of a degree of
    latitude. Without the correction the ring samples an ellipse, not a circle."""
    lat, lng, r = 47.6062, -122.3321, 1000
    pts = {p.bearing_deg: p for p in ring_points(lat, lng, rings=(r,))}
    north, east = pts[0], pts[90]

    dy = (north.lat - lat) * 111_320
    dx = (east.lng - lng) * 111_320 * math.cos(math.radians(lat))
    assert dy == pytest.approx(r, rel=0.02)
    assert dx == pytest.approx(r, rel=0.02)


def test_rings_are_recorded_on_each_point() -> None:
    assert {p.ring_m for p in ring_points(47.6, -122.3)} == {0, *DEFAULT_RINGS}


# ==========================================================================
# field classification
# ==========================================================================
@pytest.mark.parametrize("field,expected", [
    ("nearest_transmission_line_voltage_kv", "connectable"),
    ("nearest_substation_distance_m", "connectable"),
    ("nearest_gas_pipeline_distance_m", "connectable"),
    ("slope_degrees", "intrinsic"),
    ("intersects_protected_area", "intrinsic"),
    ("in_karst_area", "intrinsic"),
    ("design_wet_bulb_temperature_0_4pct_degc", "regional"),
    ("egrid_co2_output_rate_kg_per_mwh", "regional"),
])
def test_field_classes(field: str, expected: str) -> None:
    assert classify(field) == expected


def test_unknown_distance_fields_infer_as_connectable() -> None:
    """`nearest_*_distance_m` is the dominant catalog pattern, and it is exactly the
    class point sampling got wrong — so the fallback must catch it."""
    p = policy_for("nearest_something_we_have_not_seen_distance_m")
    assert p.cls == "connectable" and p.direction == "min_is_best"


# ==========================================================================
# aggregation by class
# ==========================================================================
def obs(value, ring_m, status="ok", bearing=0):
    return Observation(value, status, ring_m, bearing)


def test_west_seattle_transmission_is_found() -> None:
    """REGRESSION — the case that motivated vicinity sampling.

    The centroid returns `absent` for transmission voltage. Four ring points find real
    lines, the best 230 kV. Scored as a single point this site came out at 0.277 and was
    published as `quiet`.
    """
    samples = [
        obs(None, 0, status="absent"),
        *[obs(None, 250, status="absent") for _ in range(8)],
        *[obs(None, 750, status="absent") for _ in range(6)],
        obs(115.0, 750), obs(115.0, 750),
        *[obs(None, 1500, status="absent") for _ in range(6)],
        obs(230.0, 1500), obs(115.0, 1500),
    ]
    a = aggregate("nearest_transmission_line_voltage_kv", samples)

    assert a["class"] == "connectable"
    assert a["best"] == 230.0            # the centroid alone said "no transmission"
    assert a["best_at_m"] == 1500
    assert a["n_with_value"] == 4
    assert "search-radius artefact" in a["coverage_note"]


def test_absent_everywhere_stays_a_real_no_data_answer() -> None:
    """The tri-state distinction must survive aggregation. Absent at EVERY point is a
    genuine "nothing here"; absent at some is a coverage artefact. Collapsing the two
    would trade one false quiet for another."""
    a = aggregate("nearest_transmission_line_voltage_kv",
                  [obs(None, r, status="absent") for r in (0, 250, 750, 1500)])
    assert a["best"] is None
    assert "absent at every sample point" in a["coverage_note"]
    assert "artefact" not in a["coverage_note"]


def test_failed_samples_are_not_counted_as_answers() -> None:
    a = aggregate("slope_degrees",
                  [obs(3.0, 0), obs(None, 250, status="failed"), obs(9.0, 750)])
    assert a["n_samples"] == 3 and a["n_answers"] == 2


def test_connectable_takes_the_best_reachable_value() -> None:
    """You reach infrastructure; you do not own it. Nearest-anywhere is correct."""
    a = aggregate("nearest_substation_distance_m",
                  [obs(9000.0, 0), obs(1346.0, 1500), obs(4286.0, 750)])
    assert a["best"] == 1346.0 and a["best_at_m"] == 1500


def test_intrinsic_reports_a_distribution_not_a_number() -> None:
    """A flat pad 1.5 km away does nothing for a steep parcel, so intrinsic fields expose
    best, worst AND the usable fraction — we cannot know the real parcel boundary."""
    a = aggregate("slope_degrees",
                  [obs(v, 750) for v in (0.7, 2.1, 4.0, 8.0, 12.0, 30.0, 43.7)])

    assert a["class"] == "intrinsic"
    assert a["best"] == 0.7 and a["worst"] == 43.7
    assert a["fraction_usable"] == pytest.approx(4 / 7, abs=0.01)  # <= 10 degrees
    assert a["spread"] == pytest.approx(43.0, abs=0.1)


def test_boolean_constraint_treats_false_as_best() -> None:
    """REGRESSION. `false_is_best` fields coerce to 0/1, and grouping them with
    max_is_best reported "protected land found" as the BEST outcome — inverted."""
    a = aggregate("intersects_protected_area",
                  [obs(True, 0), obs(False, 750), obs(False, 1500)])
    assert a["best"] == 0.0 and a["worst"] == 1.0
    assert a["fraction_usable"] == pytest.approx(2 / 3, abs=0.01)


def test_categorical_prefers_the_centroid_value() -> None:
    """For a categorical intrinsic field the centroid is the site's own answer; the ring
    only says how uniform the surroundings are."""
    a = aggregate("land_use_class",
                  [obs("Developed", 0), obs("Forest", 750), obs("Forest", 1500)])
    assert a["best"] == "Developed"
    assert a["spread"] == 2
    assert a["distribution"] == {"Developed": 1, "Forest": 2}


def test_summarise_walks_a_whole_scan() -> None:
    class Rec:
        def __init__(self, v, s="ok"):
            self.value, self.status = v, s

    pts = ring_points(47.6, -122.3, rings=(250,))
    paired = [(p, {"slope_degrees": Rec(1.0 if p.is_centroid else 20.0)}) for p in pts]
    out = summarise(paired)

    assert out["slope_degrees"]["best"] == 1.0
    assert out["slope_degrees"]["worst"] == 20.0
    assert out["slope_degrees"]["n_samples"] == len(pts)
