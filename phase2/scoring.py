"""Derived optionality scores.

The brief's central move: *"No field is called `had_data_center_optionality` -- derive
it."* These metrics are a pure function of the physical facts at a coordinate. No event
ever enters, and the output is a CAPABILITY measure ("could this ground host a data
centre"), never materiality ("does this moratorium matter here") -- that second question
needs the event and belongs to Phase 3.

**Composition is a weighted geometric mean, and that is not overridable.** A profile may
change weights and thresholds; it may not make the composition additive. Under additive
weights, flat unpowered farmland scores about half marks and Phase 3 alerts on ground
that never had the option -- exactly the failure the brief calls "the keyword feed".
Multiplicatively it scores zero, which is correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlmodel import Session

from .models import Datapoint
from .store import serialize

# ---------------------------------------------------------------------------
# banding helpers
# ---------------------------------------------------------------------------
def _band(value: float, bands: list[tuple[float, float]]) -> float:
    """`bands` is [(threshold, score), ...] descending by threshold."""
    for threshold, score in bands:
        if value >= threshold:
            return score
    return bands[-1][1] if bands else 0.0


def _inverse_band(value: float, bands: list[tuple[float, float]]) -> float:
    """`bands` is [(ceiling, score), ...] ascending by ceiling. Lower is better."""
    for ceiling, score in bands:
        if value <= ceiling:
            return score
    return 0.0


def _decay(distance_m: float, full_m: float, zero_m: float) -> float:
    """1.0 within `full_m`, linearly to 0.0 at `zero_m`."""
    if distance_m <= full_m:
        return 1.0
    if distance_m >= zero_m:
        return 0.0
    return 1.0 - (distance_m - full_m) / (zero_m - full_m)


# ---------------------------------------------------------------------------
# component result
# ---------------------------------------------------------------------------
@dataclass
class Component:
    score: float
    basis: str
    fields_used: list[str]
    fields_missing: list[str]


def _val(dps: dict[str, Datapoint], name: str) -> tuple[Any, Optional[Datapoint]]:
    """Fetch a value, distinguishing the three statuses.

    `absent` returns (None, row) with the row present -- it is a real answer meaning
    "nothing here", NOT a missing field. Callers must treat it as evidence.
    """
    dp = dps.get(name)
    if dp is None:
        return None, None
    return dp.value, dp


# ---------------------------------------------------------------------------
# components
# ---------------------------------------------------------------------------
def _power(dps: dict[str, Datapoint], th: dict) -> Component:
    used, missing = [], []
    kv, kv_dp = _val(dps, "nearest_transmission_line_voltage_kv")
    dist, dist_dp = _val(dps, "nearest_substation_distance_m")

    if kv_dp is None:
        missing.append("nearest_transmission_line_voltage_kv")
    else:
        used.append("nearest_transmission_line_voltage_kv")
    if dist_dp is None:
        missing.append("nearest_substation_distance_m")
    else:
        used.append("nearest_substation_distance_m")

    if kv is None:
        # absent or missing: no known transmission. Absent is a real "nothing here".
        basis = "no transmission line reported" if kv_dp else "transmission voltage unknown"
        return Component(0.05 if kv_dp else 0.5, basis, used, missing)

    voltage_score = _band(float(kv), th["voltage_bands"])
    prox = 1.0
    if dist is not None:
        prox = _decay(float(dist), th["substation_full_m"], th["substation_zero_m"])
        basis = f"{kv:g} kV, substation {float(dist)/1000:.1f} km"
    else:
        basis = f"{kv:g} kV, substation distance unknown"

    return Component(voltage_score * max(prox, th["substation_floor"]), basis, used, missing)


def _fiber(dps: dict[str, Datapoint], th: dict) -> Component:
    available, dp = _val(dps, "fiber_broadband_available")
    if dp is None:
        return Component(0.5, "fiber unknown", [], ["fiber_broadband_available"])
    if available is None:
        return Component(th["fiber_absent"], "no fiber reported", ["fiber_broadband_available"], [])
    score = 1.0 if available else th["fiber_absent"]
    return Component(score, f"fiber_broadband_available = {available}",
                     ["fiber_broadband_available"], [])


def _terrain(dps: dict[str, Datapoint], th: dict) -> Component:
    slope, dp = _val(dps, "slope_degrees")
    if dp is None:
        return Component(0.5, "slope unknown", [], ["slope_degrees"])
    if slope is None:
        return Component(0.5, "slope absent", ["slope_degrees"], [])
    return Component(
        _inverse_band(float(slope), th["slope_bands"]),
        f"slope {float(slope):.1f} deg",
        ["slope_degrees"],
        [],
    )


def _clear(dps: dict[str, Datapoint], th: dict) -> Component:
    """Multiplicative penalty for constraints that block building."""
    score, notes, used, missing = 1.0, [], [], []
    checks = [
        ("within_floodplain_polygon", th["floodplain_penalty"], "in floodplain"),
        ("intersects_wetland", th["wetland_penalty"], "wetland"),
        ("intersects_protected_area", th["protected_penalty"], "protected area"),
    ]
    for name, penalty, label in checks:
        value, dp = _val(dps, name)
        if dp is None:
            missing.append(name)
            score *= th["unknown_penalty"]
            notes.append(f"{label} unknown")
            continue
        used.append(name)
        if value:
            score *= penalty
            notes.append(label)
    return Component(score, "; ".join(notes) if notes else "no blocking constraints",
                     used, missing)


def _access(dps: dict[str, Datapoint], th: dict) -> Component:
    dist, dp = _val(dps, "nearest_major_road_distance_m")
    if dp is None:
        return Component(0.5, "road distance unknown", [], ["nearest_major_road_distance_m"])
    if dist is None:
        return Component(th["road_absent"], "no major road reported",
                         ["nearest_major_road_distance_m"], [])
    return Component(
        _decay(float(dist), th["road_full_m"], th["road_zero_m"]),
        f"major road {float(dist)/1000:.1f} km",
        ["nearest_major_road_distance_m"],
        [],
    )


# ---------------------------------------------------------------------------
# metric definitions
# ---------------------------------------------------------------------------
COMPONENTS: dict[str, Callable[[dict[str, Datapoint], dict], Component]] = {
    "power": _power,
    "fiber": _fiber,
    "terrain": _terrain,
    "clear": _clear,
    "access": _access,
}

DEFAULT_THRESHOLDS: dict[str, Any] = {
    # >=230 kV full, 115-230 high, 69-115 partial, below that near zero
    "voltage_bands": [(230, 1.0), (115, 0.85), (69, 0.5), (0, 0.15)],
    "substation_full_m": 3000,
    "substation_zero_m": 25000,
    "substation_floor": 0.2,
    "fiber_absent": 0.15,
    # <5 deg full, 5-10 partial, 10-15 poor, >15 near zero
    "slope_bands": [(5, 1.0), (10, 0.75), (15, 0.35), (25, 0.1)],
    "floodplain_penalty": 0.35,
    "wetland_penalty": 0.3,
    "protected_penalty": 0.05,
    "unknown_penalty": 0.7,
    "road_full_m": 1500,
    "road_zero_m": 15000,
    "road_absent": 0.2,
}

#: metric -> GRADED component weights, combined as a weighted geometric mean.
#: Weights are normalised, so they express relative importance. Fiber is absent from
#: BESS entirely -- a battery does not need it.
DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    "data_center_optionality": {"power": 0.50, "fiber": 0.25, "terrain": 0.25},
    "bess_optionality": {"power": 0.65, "terrain": 0.35},
    "buildability": {"terrain": 0.60, "access": 0.40},
}

#: Components applied as a DIRECT multiplier on the graded result, not as a weighted
#: term. `clear` covers hard constraints -- floodplain, wetland, protected area -- and
#: a protected area genuinely disqualifies development however good the power is.
#: Folding it into the weighted mean would soften a 0.05 penalty to ~0.55 overall,
#: which is not what "you cannot build here" means.
PENALTY_COMPONENTS: tuple[str, ...] = ("clear",)

METRICS = tuple(DEFAULT_WEIGHTS)

_PER_COMPONENT = {
    "power": ["nearest_transmission_line_voltage_kv", "nearest_substation_distance_m"],
    "fiber": ["fiber_broadband_available"],
    "terrain": ["slope_degrees"],
    "clear": ["within_floodplain_polygon", "intersects_wetland", "intersects_protected_area"],
    "access": ["nearest_major_road_distance_m"],
}


def required_fields(metric: str) -> list[str]:
    """Which fields a metric reads. Used to decide what to fetch."""
    out: list[str] = []
    for component in list(DEFAULT_WEIGHTS[metric]) + list(PENALTY_COMPONENTS):
        for field in _PER_COMPONENT[component]:
            if field not in out:
                out.append(field)
    return out


def _confidence(fields_missing: list[str], datapoints: dict[str, Datapoint]) -> str:
    if fields_missing:
        return "low" if len(fields_missing) > 2 else "medium"
    lows = sum(1 for dp in datapoints.values() if dp.confidence in ("low", "unknown"))
    if lows > len(datapoints) / 2:
        return "medium"
    return "high"


def score(
    metric: str,
    datapoints: list[Datapoint],
    *,
    weights: Optional[dict[str, float]] = None,
    thresholds: Optional[dict[str, Any]] = None,
    profile_name: str = "default",
) -> dict[str, Any]:
    """Compute a derived metric. Composition is a weighted geometric mean (not overridable)."""
    if metric not in DEFAULT_WEIGHTS:
        raise KeyError(f"unknown metric {metric!r}; known: {', '.join(METRICS)}")

    dps = {dp.field_name: dp for dp in datapoints}
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    w = {**DEFAULT_WEIGHTS[metric], **(weights or {})}
    w = {k: v for k, v in w.items() if k in DEFAULT_WEIGHTS[metric]}
    total_weight = sum(w.values()) or 1.0

    components: dict[str, Any] = {}
    used: list[str] = []
    missing: list[str] = []
    product = 1.0

    # Graded components -> weighted geometric mean. A zero here zeroes the whole score.
    for name, weight in w.items():
        comp = COMPONENTS[name](dps, th)
        components[name] = {
            "score": round(comp.score, 4),
            "weight": round(weight / total_weight, 4),
            "basis": comp.basis,
        }
        used.extend(comp.fields_used)
        missing.extend(comp.fields_missing)
        product *= max(comp.score, 0.0) ** (weight / total_weight)

    # Penalty components -> direct multiplier. "You cannot build here" is not a
    # weighted opinion, so it is not softened by the mean.
    for name in PENALTY_COMPONENTS:
        comp = COMPONENTS[name](dps, th)
        components[name] = {
            "score": round(comp.score, 4),
            "weight": None,
            "role": "penalty_multiplier",
            "basis": comp.basis,
        }
        used.extend(comp.fields_used)
        missing.extend(comp.fields_missing)
        product *= max(comp.score, 0.0)

    citations = [
        {
            "field": dp.field_name,
            "source": dp.source,
            "source_url": dp.source_url,
            "license": dp.license,
            "fetched_at": serialize(dp)["fetched_at"],
        }
        for dp in datapoints
        if dp.field_name in used
    ]

    return {
        "metric": metric,
        "profile": profile_name,
        "score": round(product, 4),
        "confidence": _confidence(missing, dps),
        "composition": "weighted_geometric_mean",
        "components": components,
        "fields_used": used,
        "fields_missing": missing,
        "citations": citations,
    }
