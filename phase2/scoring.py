"""Derived optionality scores.

The brief's central move: *"No field is called `had_data_center_optionality` -- derive
it."* These metrics are a pure function of the physical facts at a coordinate. No event
ever enters, and the output is a CAPABILITY measure ("could this ground host a data
centre"), never materiality ("does this moratorium matter here") -- that second question
needs the event and belongs to Phase 3.

**Composition.** Graded components combine as a weighted geometric mean, so a zero
anywhere zeroes the whole score -- a site with no power has no data-centre optionality
however flat it is. Penalty components apply as a DIRECT multiplier afterwards, because
"you cannot build here" is not a weighted opinion. A profile may change weights and
thresholds; it may not change this structure.

**Every threshold below is sourced from Mireye's own `interpretation_hints`** and cites
it inline. An earlier version of this module invented its bands, which produced two real
errors: a municipal golf course scored as a hard disqualifier (GAP-4 land read as
protected), and slope penalised from 10 degrees when the documented construction
threshold is 25. Do not add a band here without a source.

**These weights are PROVISIONAL.** They will be refitted against real existing
data-centre sites. Keep every number a parameter -- see `feature_row()` for the export
that feeds that calibration.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Optional

from .models import Datapoint
from .store import serialize


# ---------------------------------------------------------------------------
# banding helpers
# ---------------------------------------------------------------------------
def _band(value: float, bands: list) -> float:
    """`bands` = [(threshold, score), ...] descending. Higher value is better."""
    for threshold, sc in bands:
        if value >= threshold:
            return sc
    return bands[-1][1] if bands else 0.0


def _inverse_band(value: float, bands: list) -> float:
    """`bands` = [(ceiling, score), ...] ascending. Lower value is better."""
    for ceiling, sc in bands:
        if value <= ceiling:
            return sc
    return bands[-1][1] if bands else 0.0


def _decay(distance_m: float, full_m: float, zero_m: float) -> float:
    if distance_m <= full_m:
        return 1.0
    if distance_m >= zero_m:
        return 0.0
    return 1.0 - (distance_m - full_m) / (zero_m - full_m)


@dataclass
class Component:
    score: float
    basis: str
    fields_used: list = dc_field(default_factory=list)
    fields_missing: list = dc_field(default_factory=list)


class _Reader:
    """Reads datapoints while preserving Mireye's tri-state semantics.

    `get()` returns (value, present) where `present` distinguishes:
      * a real answer, including `absent` ("the source says nothing here")
      * a field we never fetched
    Conflating these inverts decisions, so the split is explicit everywhere.
    """

    def __init__(self, dps: dict):
        self.dps = dps
        self.used: list = []
        self.missing: list = []

    def get(self, name: str):
        dp = self.dps.get(name)
        if dp is None:
            self.missing.append(name)
            return None, False
        self.used.append(name)
        return dp.value, True


# ---------------------------------------------------------------------------
# components
# ---------------------------------------------------------------------------
def _power(r: _Reader, th: dict) -> Component:
    """Grid access.

    Mireye: ">=230 kV = transmission-grade, <100 kV = sub-transmission" and "[nearest]
    may be LOWER than max_transmission_line_voltage_kv_within_radius if a higher-voltage
    line runs slightly farther; read both" -- so we take the better of the two.
    Substation: "within a few km is materially cheaper/faster; >10-20km often kills a
    site."
    """
    kv, kv_present = r.get("nearest_transmission_line_voltage_kv")
    kv_max, _ = r.get("max_transmission_line_voltage_kv_within_radius")
    vclass, _ = r.get("nearest_transmission_line_voltage_class")
    sub, sub_present = r.get("nearest_substation_distance_m")

    best = max([v for v in (kv, kv_max) if v is not None], default=None)

    if best is None:
        # A line may exist with unpublished voltage -- voltage_class tells us which.
        if vclass:
            return Component(th["voltage_unpublished"],
                             f"line present, voltage class {vclass}", r.used, r.missing)
        if kv_present:
            return Component(th["voltage_absent"],
                             "no transmission line within search radius", r.used, r.missing)
        return Component(0.5, "transmission voltage not fetched", r.used, r.missing)

    volt = _band(float(best), th["voltage_bands"])
    note = f"{best:g} kV"
    if kv is not None and kv_max is not None and kv_max > kv:
        note = f"{kv_max:g} kV within radius ({kv:g} kV nearest)"

    prox = 1.0
    if sub is not None:
        prox = max(_decay(float(sub), th["substation_full_m"], th["substation_zero_m"]),
                   th["substation_floor"])
        note += f", substation {float(sub) / 1000:.1f} km"
    elif sub_present:
        note += ", no substation in range"
        prox = th["substation_floor"]

    return Component(volt * prox, note, r.used, r.missing)


def _interconnect(r: _Reader, th: dict) -> Component:
    """Carrier connectivity.

    Mireye is explicit that `fiber_broadband_available` is mass-market consumer FTTP and
    "an availability FLOOR, NOT an interconnect-ecosystem signal". Long-haul fibre
    "overwhelmingly follows Class-I mainline rail rights-of-way", so corridor proximity
    is the closer proxy for backhaul reach. Treated as a floor plus two boosts rather
    than a single binary.
    """
    fiber, fiber_present = r.get("fiber_broadband_available")
    providers, _ = r.get("fiber_provider_count")
    rail, _ = r.get("nearest_long_haul_rail_corridor_distance_m")

    if not fiber_present and rail is None:
        return Component(0.5, "connectivity not fetched", r.used, r.missing)

    base = th["fiber_present"] if fiber else th["fiber_absent"]
    notes = [f"fttp={fiber}"]

    if providers is not None:
        base = min(1.0, base + th["per_provider_bonus"] * max(0, int(providers) - 1))
        notes.append(f"{providers} providers")
    if rail is not None:
        corridor = _decay(float(rail), th["rail_full_m"], th["rail_zero_m"])
        base = min(1.0, base * (th["rail_floor"] + (1 - th["rail_floor"]) * corridor))
        notes.append(f"long-haul corridor {float(rail) / 1000:.1f} km")

    return Component(base, ", ".join(notes), r.used, r.missing)


def _terrain(r: _Reader, th: dict) -> Component:
    """Buildability of the ground.

    Mireye: "Slope >25 deg complicates conventional construction." An earlier version
    penalised from 10 degrees, which materially understated ordinary urban hillsides.
    """
    slope, present = r.get("slope_degrees")
    if slope is None:
        return Component(0.5 if not present else th["slope_absent"],
                         "slope unknown" if not present else "slope not reported",
                         r.used, r.missing)
    return Component(_inverse_band(float(slope), th["slope_bands"]),
                     f"slope {float(slope):.1f} deg", r.used, r.missing)


def _cooling(r: _Reader, th: dict) -> Component:
    """Evaporative-cooling viability.

    Mireye calls design wet bulb "THE variable for evaporative-cooling viability:
    >~25C = low evap potential (oversize towers, less water saving); low = water-side
    economization + lower cooling capex."
    """
    wb, present = r.get("design_wet_bulb_temperature_0_4pct_degc")
    hot, _ = r.get("days_above_32c_annual_count")
    if wb is None:
        return Component(0.5, "wet bulb not fetched" if not present else "wet bulb absent",
                         r.used, r.missing)
    sc = _inverse_band(float(wb), th["wet_bulb_bands"])
    note = f"design wet bulb {float(wb):.1f}C"
    if hot is not None:
        sc *= _inverse_band(float(hot), th["hot_day_bands"])
        note += f", {int(hot)} days >32C"
    return Component(sc, note, r.used, r.missing)


def _water(r: _Reader, th: dict) -> Component:
    """Cooling-water availability.

    `within_water_service_area` is a mapped EPA CWS boundary -- "a strong sign municipal
    water service exists there; it is NOT a will-serve commitment". High
    `surface_water_supply_use_index_huc12` "flags watersheds where consumptive use
    approaches supply (permitting/curtailment risk)".
    """
    served, present = r.get("within_water_service_area")
    sui, _ = r.get("surface_water_supply_use_index_huc12")
    if not present and sui is None:
        return Component(0.5, "water not fetched", r.used, r.missing)

    sc = th["water_served"] if served else th["water_unserved"]
    note = f"municipal water={served}"
    if sui is not None:
        sc *= _inverse_band(float(sui), th["sui_bands"])
        note += f", supply-use index {float(sui):.2f}"
    return Component(min(sc, 1.0), note, r.used, r.missing)


def _cost(r: _Reader, th: dict) -> Component:
    """Industrial power price. Availability is not affordability."""
    price, present = r.get("avg_retail_electricity_price_industrial_usd_per_kwh")
    if price is None:
        return Component(0.5, "power price not fetched" if not present else "price absent",
                         r.used, r.missing)
    return Component(_inverse_band(float(price), th["price_bands"]),
                     f"${float(price):.3f}/kWh industrial", r.used, r.missing)


def _access(r: _Reader, th: dict) -> Component:
    dist, present = r.get("nearest_major_road_distance_m")
    if dist is None:
        return Component(0.5 if not present else th["road_absent"],
                         "road distance unknown", r.used, r.missing)
    return Component(_decay(float(dist), th["road_full_m"], th["road_zero_m"]),
                     f"major road {float(dist) / 1000:.1f} km", r.used, r.missing)


def _clear(r: _Reader, th: dict) -> Component:
    """Hard constraints, applied as a direct multiplier.

    Protected land is scaled by GAP status, not by the bare intersects flag. Mireye:
    "1/2 = real conservation protection ... 3 = protected from conversion but extractive
    uses allowed; 4 ~ nominal -- **Do NOT overstate GAP 4 as a development constraint**."
    The bare flag alone scored a municipal golf course as disqualifying.

    Note the asymmetry Mireye flags: "a private target parcel will NOT appear here" --
    so this field can produce false positives but never false negatives.
    """
    sc, notes = 1.0, []

    protected, prot_present = r.get("intersects_protected_area")
    gap, _ = r.get("protected_area_gap_status")
    name, _ = r.get("protected_area_name")
    if protected:
        if gap is not None:
            penalty = th["gap_penalties"].get(str(int(gap)), th["gap_default"])
            label = f"GAP {int(gap)}"
        else:
            penalty, label = th["gap_default"], "protected, GAP unknown"
        sc *= penalty
        notes.append(f"{label}{f' ({name})' if name else ''}")
    elif prot_present:
        notes.append("not protected")

    flood, _ = r.get("within_floodplain_polygon")
    if flood:
        sc *= th["floodplain_penalty"]
        notes.append("floodplain")

    wet, _ = r.get("intersects_wetland")
    if wet:
        sc *= th["wetland_penalty"]
        notes.append("wetland")

    # Nonattainment "triggers stricter New Source Review / emission-offset permitting
    # for combustion equipment -- directly raises permitting cost and timeline for
    # on-site gas generation and backup diesel at a data center." Friction, not a ban.
    naa, _ = r.get("in_air_quality_nonattainment")
    if naa:
        sc *= th["nonattainment_penalty"]
        notes.append("air-quality nonattainment")

    for missing in ("intersects_protected_area", "within_floodplain_polygon"):
        if missing in r.missing:
            sc *= th["unknown_penalty"]

    return Component(sc, "; ".join(notes) if notes else "no blocking constraints",
                     r.used, r.missing)


COMPONENTS: dict[str, Callable[[_Reader, dict], Component]] = {
    "power": _power,
    "interconnect": _interconnect,
    "terrain": _terrain,
    "cooling": _cooling,
    "water": _water,
    "cost": _cost,
    "access": _access,
    "clear": _clear,
}

# ---------------------------------------------------------------------------
# thresholds -- every entry traceable to a Mireye interpretation_hint
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLDS: dict[str, Any] = {
    # ">=230 kV = transmission-grade, <100 kV = sub-transmission"
    "voltage_bands": [(230, 1.0), (100, 0.80), (1, 0.45)],
    "voltage_absent": 0.05,
    "voltage_unpublished": 0.45,
    # ">10-20km often kills a site"
    "substation_full_m": 3000,
    "substation_zero_m": 15000,
    "substation_floor": 0.15,
    # mass-market FTTP is a floor, not an interconnect signal
    "fiber_present": 0.80,
    "fiber_absent": 0.40,
    "per_provider_bonus": 0.07,
    "rail_full_m": 2000,
    "rail_zero_m": 30000,
    "rail_floor": 0.75,
    # "Slope >25 deg complicates conventional construction"
    "slope_bands": [(10, 1.0), (18, 0.85), (25, 0.55), (35, 0.2), (90, 0.05)],
    "slope_absent": 0.5,
    # ">~25C = low evap potential"
    "wet_bulb_bands": [(16, 1.0), (19, 0.92), (22, 0.75), (25, 0.5), (99, 0.28)],
    "hot_day_bands": [(10, 1.0), (40, 0.92), (90, 0.8), (400, 0.65)],
    # mapped CWS boundary is a strong sign, not a will-serve commitment
    "water_served": 1.0,
    "water_unserved": 0.55,
    # "high SUI flags watersheds where consumptive use approaches supply"
    "sui_bands": [(0.1, 1.0), (0.4, 0.9), (0.8, 0.7), (999, 0.45)],
    "price_bands": [(0.05, 1.0), (0.08, 0.9), (0.12, 0.7), (0.20, 0.45), (99, 0.25)],
    # GAP 1/2 real protection; 3 extractive allowed; 4 nominal -- do NOT overstate
    "gap_penalties": {"1": 0.03, "2": 0.20, "3": 0.65, "4": 0.95},
    "gap_default": 0.35,
    "floodplain_penalty": 0.40,
    "wetland_penalty": 0.35,
    "nonattainment_penalty": 0.80,
    "unknown_penalty": 0.85,
    "road_full_m": 1500,
    "road_zero_m": 15000,
    "road_absent": 0.2,
}

#: metric -> GRADED component weights (weighted geometric mean). PROVISIONAL.
DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    "data_center_optionality": {
        "power": 0.40, "interconnect": 0.20, "terrain": 0.15,
        "cooling": 0.15, "water": 0.10,
    },
    # A battery needs grid and ground. It does not need fibre, cooling water, or
    # evaporative-cooling climate -- it is a price-arbitrage asset, so cost matters.
    "bess_optionality": {"power": 0.60, "terrain": 0.25, "cost": 0.15},
    "buildability": {"terrain": 0.55, "access": 0.30, "water": 0.15},
}

#: applied as a direct multiplier after the weighted mean, for every metric
PENALTY_COMPONENTS: tuple = ("clear",)

METRICS = tuple(DEFAULT_WEIGHTS)

_PER_COMPONENT = {
    "power": ["nearest_transmission_line_voltage_kv", "max_transmission_line_voltage_kv_within_radius",
              "nearest_transmission_line_voltage_class", "nearest_substation_distance_m"],
    "interconnect": ["fiber_broadband_available", "fiber_provider_count",
                     "nearest_long_haul_rail_corridor_distance_m"],
    "terrain": ["slope_degrees"],
    "cooling": ["design_wet_bulb_temperature_0_4pct_degc", "days_above_32c_annual_count"],
    "water": ["within_water_service_area", "surface_water_supply_use_index_huc12"],
    "cost": ["avg_retail_electricity_price_industrial_usd_per_kwh"],
    "access": ["nearest_major_road_distance_m"],
    "clear": ["intersects_protected_area", "protected_area_gap_status", "protected_area_name",
              "within_floodplain_polygon", "intersects_wetland", "in_air_quality_nonattainment"],
}


def required_fields(metric: str) -> list:
    out: list = []
    for component in list(DEFAULT_WEIGHTS[metric]) + list(PENALTY_COMPONENTS):
        for f in _PER_COMPONENT[component]:
            if f not in out:
                out.append(f)
    return out


def all_feature_fields() -> list:
    """Every raw field any metric reads -- the column set for calibration export."""
    out: list = []
    for fields in _PER_COMPONENT.values():
        for f in fields:
            if f not in out:
                out.append(f)
    return out


def _confidence(missing: list, dps: dict) -> str:
    if len(missing) > 3:
        return "low"
    if missing:
        return "medium"
    lows = sum(1 for dp in dps.values() if dp.confidence in ("low", "unknown"))
    return "medium" if lows > len(dps) / 2 else "high"


def score(
    metric: str,
    datapoints: list,
    *,
    weights: Optional[dict] = None,
    thresholds: Optional[dict] = None,
    profile_name: str = "default",
) -> dict:
    if metric not in DEFAULT_WEIGHTS:
        raise KeyError(f"unknown metric {metric!r}; known: {', '.join(METRICS)}")

    dps = {dp.field_name: dp for dp in datapoints}
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    w = {k: v for k, v in {**DEFAULT_WEIGHTS[metric], **(weights or {})}.items()
         if k in DEFAULT_WEIGHTS[metric]}
    total = sum(w.values()) or 1.0

    components: dict = {}
    used: list = []
    missing: list = []
    product = 1.0

    for name, weight in w.items():
        r = _Reader(dps)
        comp = COMPONENTS[name](r, th)
        components[name] = {"score": round(comp.score, 4),
                            "weight": round(weight / total, 4), "basis": comp.basis}
        used += comp.fields_used
        missing += comp.fields_missing
        product *= max(comp.score, 0.0) ** (weight / total)

    for name in PENALTY_COMPONENTS:
        r = _Reader(dps)
        comp = COMPONENTS[name](r, th)
        components[name] = {"score": round(comp.score, 4), "weight": None,
                            "role": "penalty_multiplier", "basis": comp.basis}
        used += comp.fields_used
        missing += comp.fields_missing
        product *= max(comp.score, 0.0)

    used = list(dict.fromkeys(used))
    missing = list(dict.fromkeys(missing))

    return {
        "metric": metric,
        "profile": profile_name,
        "score": round(product, 4),
        "confidence": _confidence(missing, dps),
        "composition": "weighted_geometric_mean",
        "calibration": "provisional — weights not yet fitted against real sites",
        "components": components,
        "fields_used": used,
        "fields_missing": missing,
        "citations": [
            {"field": dp.field_name, "source": dp.source, "source_url": dp.source_url,
             "license": dp.license, "fetched_at": serialize(dp)["fetched_at"]}
            for dp in datapoints if dp.field_name in used
        ],
    }


def feature_row(site_label: str, datapoints: list, *, extra: Optional[dict] = None) -> dict:
    """One flat row per site: raw values plus component sub-scores.

    This is the export that feeds weight calibration against real data-centre sites.
    Raw values are included alongside sub-scores so the fitting can bypass the bands
    entirely if the evidence says they are wrong.
    """
    dps = {dp.field_name: dp for dp in datapoints}
    row: dict = {"site": site_label}
    row.update(extra or {})

    for f in all_feature_fields():
        dp = dps.get(f)
        row[f] = None if dp is None else dp.value
        row[f"{f}__status"] = None if dp is None else dp.status

    th = DEFAULT_THRESHOLDS
    for name, fn in COMPONENTS.items():
        row[f"cmp_{name}"] = round(fn(_Reader(dps), th).score, 4)
    for metric in METRICS:
        row[f"score_{metric}"] = score(metric, datapoints)["score"]
    return row
