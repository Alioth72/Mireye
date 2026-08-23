"""Vicinity sampling — the measurement unit for Phase 2.

Build Brief II says *"one vicinity fetch"* and *"everything within reach of that
coordinate."* Phase 2 originally sampled a single point, which produced a wrong headline
result: West Seattle bluff scored 0.277 and was reported `quiet` because
`nearest_transmission_line_voltage_kv` returned `absent` at one centroid. A 25-point ring
around the same coordinate found **230 kV at 1.3 km**.

Point sampling is **asymmetric**: it can only ever under-report proximity, never
over-report it, so it manufactures false quiets across every `nearest_*` field. A false
quiet is the worst failure this product can make — the landowner is never warned.

**The ring is not the owner's parcel.** Conflating the two was the original mistake, and
it is why fields fall into three classes:

* ``connectable`` — infrastructure you *reach* rather than own (transmission, substation,
  pipeline, road). Best across the ring is correct; distance is the cost.
* ``intrinsic`` — properties of the ground itself (slope, floodplain, protected status).
  A flat pad 1.5 km away does nothing for a steep parcel, so these report a
  *distribution*: best, worst, and the fraction of sampled ground that is usable.
* ``regional`` — uniform at this scale (climate, grid carbon, incentives). Centroid only.

We cannot know the true parcel boundary — parcel fields cost 300 credits per location and
the brief puts them out of scope. That is exactly why an intrinsic field must never
collapse to one number: a fraction plus a spread is honest where a single value pretends
to describe a hundred acres.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Literal, NamedTuple, Optional

#: metres per degree of latitude (WGS84 mean); good to ~0.5% at the scales we sample
_M_PER_DEG_LAT = 111_320.0

FieldClass = Literal["connectable", "intrinsic", "regional"]
Direction = Literal["min_is_best", "max_is_best", "false_is_best", "categorical"]

#: Default ring geometry. Centroid + 8 bearings x 3 rings = exactly 25 locations, which
#: is the hard cap for one `/v1/fetch/batch` call. Adding a ring means two calls and
#: breaks the one-call-per-vicinity property -- do not widen this casually.
DEFAULT_RINGS: tuple = (250, 750, 1500)
DEFAULT_BEARINGS = 8
MAX_BATCH_LOCATIONS = 25


class SamplePoint(NamedTuple):
    lat: float
    lng: float
    ring_m: int
    bearing_deg: int

    @property
    def is_centroid(self) -> bool:
        return self.ring_m == 0


def ring_points(
    lat: float,
    lng: float,
    *,
    rings: Iterable[int] = DEFAULT_RINGS,
    bearings: int = DEFAULT_BEARINGS,
) -> list:
    """Centroid plus evenly spaced points on each ring.

    Bearings run clockwise from north. Longitude is latitude-corrected, which matters
    at Seattle's 47.6 degrees where a degree of longitude is only ~67% of a degree of
    latitude — an uncorrected offset would sample an ellipse, not a circle.
    """
    points = [SamplePoint(lat, lng, 0, 0)]
    cos_lat = math.cos(math.radians(lat))
    for ring_m in rings:
        for i in range(bearings):
            bearing = i * (360 // bearings)
            theta = math.radians(bearing)
            dlat = (ring_m * math.cos(theta)) / _M_PER_DEG_LAT
            dlng = (ring_m * math.sin(theta)) / (_M_PER_DEG_LAT * cos_lat)
            points.append(
                SamplePoint(round(lat + dlat, 6), round(lng + dlng, 6), ring_m, bearing)
            )
    if len(points) > MAX_BATCH_LOCATIONS:
        raise ValueError(
            f"{len(points)} points exceeds the {MAX_BATCH_LOCATIONS}-location batch cap"
        )
    return points


# ---------------------------------------------------------------------------
# field classification
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Policy:
    cls: FieldClass
    direction: Direction
    #: for intrinsic numerics: the value at or below/above which ground counts as usable
    good_threshold: Optional[float] = None


#: Explicit policies. Anything not listed falls through to `_infer_policy`, which reads
#: the field-name convention; the explicit table exists for fields whose name does not
#: give the direction away.
POLICIES: dict = {
    # --- connectable: you reach this, you do not own it -------------------
    "nearest_transmission_line_voltage_kv": Policy("connectable", "max_is_best"),
    "max_transmission_line_voltage_kv_within_radius": Policy("connectable", "max_is_best"),
    "nearest_transmission_line_voltage_class": Policy("connectable", "categorical"),
    "nearest_osm_substation_max_voltage_kv": Policy("connectable", "max_is_best"),
    "nearest_power_plant_capacity_mw": Policy("connectable", "max_is_best"),
    "nearest_power_plant_primary_fuel": Policy("connectable", "categorical"),
    "fiber_provider_count": Policy("connectable", "max_is_best"),
    "fiber_broadband_available": Policy("connectable", "categorical"),
    "btm_gas_candidacy_flag": Policy("connectable", "categorical"),
    "transmission_redundancy_flag": Policy("connectable", "categorical"),
    "near_epa_repowering_site": Policy("connectable", "categorical"),

    # --- intrinsic: only ground you hold counts ---------------------------
    "slope_degrees": Policy("intrinsic", "min_is_best", good_threshold=10.0),
    "elevation": Policy("intrinsic", "min_is_best"),
    "within_floodplain_polygon": Policy("intrinsic", "false_is_best"),
    "fema_flood_zone": Policy("intrinsic", "categorical"),
    "intersects_wetland": Policy("intrinsic", "false_is_best"),
    "wetland_acres": Policy("intrinsic", "min_is_best", good_threshold=0.0),
    "intersects_protected_area": Policy("intrinsic", "false_is_best"),
    "protected_area_gap_status": Policy("intrinsic", "max_is_best"),
    "protected_area_name": Policy("intrinsic", "categorical"),
    "protected_area_designation": Policy("intrinsic", "categorical"),
    "intersects_critical_habitat": Policy("intrinsic", "false_is_best"),
    "intersects_conservation_easement": Policy("intrinsic", "false_is_best"),
    "landslide_susceptibility_index": Policy("intrinsic", "min_is_best", good_threshold=50.0),
    "in_karst_area": Policy("intrinsic", "false_is_best"),
    "karst_exposure_class": Policy("intrinsic", "categorical"),
    "seismic_design_category": Policy("intrinsic", "categorical"),
    "tree_canopy_pct": Policy("intrinsic", "min_is_best", good_threshold=30.0),
    "land_use_class": Policy("intrinsic", "categorical"),
    "prime_farmland_classification": Policy("intrinsic", "categorical"),
    "grading_difficulty_class": Policy("intrinsic", "categorical"),
    "wildfire_annual_frequency": Policy("intrinsic", "min_is_best", good_threshold=0.01),
    "within_water_service_area": Policy("intrinsic", "categorical"),

    # --- regional: uniform at this scale, centroid only -------------------
    "design_wet_bulb_temperature_0_4pct_degc": Policy("regional", "min_is_best"),
    "days_above_32c_annual_count": Policy("regional", "min_is_best"),
    "mean_annual_relative_humidity_pct": Policy("regional", "min_is_best"),
    "egrid_co2_output_rate_kg_per_mwh": Policy("regional", "min_is_best"),
    "avg_retail_electricity_price_industrial_usd_per_kwh": Policy("regional", "min_is_best"),
    "natural_gas_industrial_price_usd_per_mcf": Policy("regional", "min_is_best"),
    "modeled_onsite_gas_generation_cost_usd_per_mwh": Policy("regional", "min_is_best"),
    "in_air_quality_nonattainment": Policy("regional", "false_is_best"),
    "in_air_quality_maintenance": Policy("regional", "false_is_best"),
    "air_quality_nonattainment_pollutants": Policy("regional", "categorical"),
    "air_quality_worst_classification": Policy("regional", "categorical"),
    "interconnection_queue_active_capacity_county_mw": Policy("regional", "min_is_best"),
    "surface_water_supply_use_index_huc12": Policy("regional", "min_is_best"),
    "huc12_thermoelectric_consumptive_use_m3_per_day": Policy("regional", "min_is_best"),
    "tax_incentive_stack": Policy("regional", "categorical"),
    "in_opportunity_zone": Policy("regional", "categorical"),
    "housing_units_density_per_km2": Policy("regional", "min_is_best"),
    "housing_units_within_1km": Policy("regional", "min_is_best"),
    "political_region": Policy("regional", "categorical"),
    "political_county": Policy("regional", "categorical"),
    "political_locality": Policy("regional", "categorical"),
    "tract_geoid": Policy("regional", "categorical"),
}


def _infer_policy(field: str) -> Policy:
    """Fall back to the field-name convention.

    `nearest_*_distance_m` is the dominant pattern in the catalog and is always
    connectable-and-closer-is-better, which is precisely the class that point sampling
    was getting wrong.
    """
    if field.endswith("_distance_m"):
        return Policy("connectable", "min_is_best")
    if field.endswith(("_count", "_within_radius_count")):
        return Policy("connectable", "max_is_best")
    if field.startswith(("intersects_", "within_", "in_")):
        return Policy("intrinsic", "false_is_best")
    return Policy("regional", "categorical")


def policy_for(field: str) -> Policy:
    return POLICIES.get(field) or _infer_policy(field)


def classify(field: str) -> FieldClass:
    return policy_for(field).cls


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
class Observation(NamedTuple):
    value: Any
    status: str
    ring_m: int
    bearing_deg: int


def _is_answer(o: Observation) -> bool:
    """`ok` and `absent` are both real answers; `failed` is not (see store.py)."""
    return o.status in ("ok", "absent")


def _usable(value: Any, policy: Policy) -> Optional[bool]:
    """Is this observation 'good ground' under the policy? None when undecidable."""
    if policy.direction == "false_is_best":
        return not bool(value)
    if value is None:
        return None
    if policy.direction == "min_is_best" and policy.good_threshold is not None:
        return float(value) <= policy.good_threshold
    if policy.direction == "max_is_best" and policy.good_threshold is not None:
        return float(value) >= policy.good_threshold
    return None


def aggregate(field: str, observations: Iterable) -> dict:
    """Collapse ring observations into a summary for one field.

    The tri-state distinction survives: a field `absent` at *every* sample point is a
    real "nothing here", while absent at some and present at others is a coverage
    artefact of the source's search radius — which is the West Seattle case exactly.
    """
    obs = [o for o in observations]
    answers = [o for o in obs if _is_answer(o)]
    with_values = [o for o in answers if o.value is not None]
    policy = policy_for(field)

    summary: dict = {
        "field": field,
        "class": policy.cls,
        "direction": policy.direction,
        "n_samples": len(obs),
        "n_answers": len(answers),
        "n_with_value": len(with_values),
        "best": None,
        "worst": None,
        "best_at_m": None,
        "spread": None,
        "fraction_usable": None,
        "coverage_note": None,
    }

    if not answers:
        summary["coverage_note"] = "no sample returned an answer"
        return summary

    if not with_values:
        # Every point answered, and every answer was "nothing here".
        summary["coverage_note"] = "absent at every sample point — a real no-data answer"
        return summary

    if len(with_values) < len(answers):
        summary["coverage_note"] = (
            f"present at {len(with_values)}/{len(answers)} sample points — "
            "absence at the centroid alone is a search-radius artefact, not a fact"
        )

    if policy.direction == "categorical":
        counts: dict = {}
        for o in with_values:
            counts[str(o.value)] = counts.get(str(o.value), 0) + 1
        winner = max(counts.items(), key=lambda kv: kv[1])
        centroid = next((o for o in with_values if o.ring_m == 0), None)
        summary["best"] = centroid.value if centroid is not None else winner[0]
        summary["worst"] = winner[0]
        summary["spread"] = len(counts)
        summary["distribution"] = counts
        return summary

    numeric = [(float(o.value), o) for o in with_values]
    lo_val, lo_obs = min(numeric, key=lambda t: t[0])
    hi_val, hi_obs = max(numeric, key=lambda t: t[0])

    # false_is_best shares the low-is-better direction: a boolean constraint coerces to
    # 0/1 and False (0) is the good case. Grouping it with max_is_best would report
    # 'protected land found' as the BEST outcome, which inverts the meaning.
    if policy.direction in ("min_is_best", "false_is_best"):
        summary["best"], summary["worst"] = lo_val, hi_val
        summary["best_at_m"] = lo_obs.ring_m
    else:
        summary["best"], summary["worst"] = hi_val, lo_val
        summary["best_at_m"] = hi_obs.ring_m
    summary["spread"] = round(hi_val - lo_val, 4)

    usable = [_usable(o.value, policy) for o in with_values]
    decided = [u for u in usable if u is not None]
    if decided:
        summary["fraction_usable"] = round(sum(decided) / len(decided), 3)

    return summary


def summarise(records_by_point: Iterable) -> dict:
    """Aggregate a whole vicinity scan.

    `records_by_point` yields `(SamplePoint, {field: FieldRecord})` pairs — the shape a
    batch response unpacks into.
    """
    per_field: dict = {}
    for point, records in records_by_point:
        for name, rec in records.items():
            per_field.setdefault(name, []).append(
                Observation(rec.value, rec.status, point.ring_m, point.bearing_deg)
            )
    return {name: aggregate(name, obs) for name, obs in per_field.items()}
