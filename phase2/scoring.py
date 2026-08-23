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

    def __init__(self, dps: dict, vicinity: Optional[dict] = None):
        self.dps = dps
        self.vicinity = vicinity or {}
        self.used: list = []
        self.missing: list = []

    def get(self, name: str):
        """Prefer the vicinity summary where one exists.

        For a CONNECTABLE field the summary's `best` is the right answer -- you reach
        infrastructure rather than own it, so a 230 kV line 1.3 km away is genuinely
        available even when the centroid reports `absent`. For an INTRINSIC field
        `best` is the best ground in the ring; callers wanting the spread use
        `distribution()`.
        """
        v = self.vicinity.get(name)
        if v is not None and v.get("n_answers"):
            self.used.append(name)
            return v.get("best"), True
        dp = self.dps.get(name)
        if dp is None:
            self.missing.append(name)
            return None, False
        self.used.append(name)
        return dp.value, True

    def distribution(self, name: str) -> Optional[dict]:
        """Best/worst/fraction for an intrinsic field, when a ring was sampled.

        An intrinsic field must never be reported as one number: we cannot know the
        real parcel boundary, so a single value would pretend to describe a hundred
        acres of mixed ground.
        """
        v = self.vicinity.get(name)
        if not v or v.get("fraction_usable") is None:
            return None
        return {"best": v["best"], "worst": v["worst"],
                "fraction_usable": v["fraction_usable"], "spread": v.get("spread")}



# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------
# Mireye fields are not independent inputs to an average -- several form permitting
# CHAINS where a qualifier discounts or vetoes its parent. Scoring `btm_gas_candidacy_
# flag: True` without checking Class I proximity says a gas plant is viable when it may
# not be permittable; scoring `slope_degrees` alone says flat ground is buildable when
# it may be karst. A gate is a qualifier attached to a component, applied after it.


@dataclass
class Gate:
    multiplier: float
    reason: str
    fields_used: list = dc_field(default_factory=list)


def _gate_class_i(r: _Reader, th: dict):
    """PSD / Federal Land Manager exposure for on-site combustion.

    Mireye: "A BTM gas plant clearing PSD major-source thresholds near a Class I area
    triggers mandatory Federal-Land-Manager consultation + visibility / air-quality-
    related-values modeling (customary screening ~300 km) -- a lead-time and cost
    escalator distinct from nonattainment NNSR."
    """
    dist, present = r.get("nearest_class_i_area_distance_m")
    name, _ = r.get("nearest_class_i_area_name")
    if dist is None:
        return None
    km = float(dist) / 1000
    mult = _band(km, th["class_i_bands"])
    if mult >= 1.0:
        return None
    return Gate(mult, f"Class I area {name or 'protected airshed'} at {km:.0f} km "
                      f"-- PSD/FLM consultation likely", ["nearest_class_i_area_distance_m"])


def _gate_nonattainment(r: _Reader, th: dict):
    """NNSR + emission offsets for combustion equipment in nonattainment areas."""
    naa, _ = r.get("in_air_quality_nonattainment")
    maint, _ = r.get("in_air_quality_maintenance")
    if naa:
        return Gate(th["gate_nonattainment"], "nonattainment -- NNSR + emission offsets "
                    "required for on-site combustion", ["in_air_quality_nonattainment"])
    if maint:
        return Gate(th["gate_maintenance"], "air-quality maintenance area -- added "
                    "permitting obligations", ["in_air_quality_maintenance"])
    return None


def _gate_landslide(r: _Reader, th: dict):
    """Mireye: "High values trigger geotech investigation for foundations / access roads
    / buried interconnect on slopes. Complements slope_degrees (modeled failure, not
    just steepness)." """
    idx, _ = r.get("landslide_susceptibility_index")
    if idx is None:
        return None
    mult = _inverse_band(float(idx), th["landslide_bands"])
    if mult >= 1.0:
        return None
    return Gate(mult, f"landslide susceptibility {float(idx):.0f} -- geotech required",
                ["landslide_susceptibility_index"])


def _gate_seismic(r: _Reader, th: dict):
    """Mireye: "A/B minimal detailing; D/E/F require special seismic systems + equipment
    certification + major cost/schedule." """
    cat, _ = r.get("seismic_design_category")
    if not cat:
        return None
    mult = th["seismic_categories"].get(str(cat).upper().strip())
    if mult is None or mult >= 1.0:
        return None
    return Gate(mult, f"seismic design category {cat} -- special systems and equipment "
                      "certification", ["seismic_design_category"])


def _gate_karst(r: _Reader, th: dict):
    """Mireye: karst_exposure_class is "the single most load-bearing qualifier on
    in_karst_area. exposed ... means soluble rock at or near the land surface -- the
    sinkhole-relevant case." The bare boolean is not actionable on its own.
    """
    in_karst, _ = r.get("in_karst_area")
    exposure, _ = r.get("karst_exposure_class")
    if not in_karst:
        return None
    key = str(exposure or "unknown").lower()
    mult = th["karst_exposure"].get(key, th["karst_default"])
    if mult >= 1.0:
        return None
    return Gate(mult, f"karst ({key}) -- sinkhole / foundation risk",
                ["in_karst_area", "karst_exposure_class"])


def _gate_queue(r: _Reader, th: dict):
    """Interconnection headroom.

    Mireye: "Heavy active queue = constrained headroom + study-delay risk; low queue
    near strong transmission = green-field signal." Note the direction -- a heavy queue
    is a NEGATIVE, which an earlier reading of this field had backwards.
    """
    mw, _ = r.get("interconnection_queue_active_capacity_county_mw")
    if mw is None:
        return None
    mult = _inverse_band(float(mw), th["queue_bands"])
    if mult >= 1.0:
        return None
    return Gate(mult, f"{float(mw):.0f} MW active interconnection queue in county "
                      "-- constrained headroom, study delay",
                ["interconnection_queue_active_capacity_county_mw"])


#: component -> qualifiers applied after it
GATES: dict = {
    "btm_fuel": [_gate_class_i, _gate_nonattainment],
    "terrain": [_gate_landslide, _gate_seismic, _gate_karst],
    "power": [_gate_queue],
}


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
            # Second source. EIA absent does not prove no grid -- OpenInfraMap covers
            # lines EIA omits, so a hit here means "EIA has no record", not "no line".
            osm_kv, _ = r.get("nearest_osm_substation_max_voltage_kv")
            osm_d, _ = r.get("nearest_osm_substation_distance_m")
            if osm_kv is not None or osm_d is not None:
                sc = th["voltage_absent"] + (th["osm_only_ceiling"] - th["voltage_absent"]) * (
                    _decay(float(osm_d), th["substation_full_m"], th["substation_zero_m"])
                    if osm_d is not None else 0.5)
                detail = f"{osm_kv:g} kV " if osm_kv is not None else ""
                detail += f"at {float(osm_d)/1000:.1f} km" if osm_d is not None else ""
                return Component(sc, f"no EIA transmission in range; OSM substation {detail}",
                                 r.used, r.missing)
            return Component(th["voltage_absent"],
                             "no transmission line within search radius (EIA and OSM)",
                             r.used, r.missing)
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
    note = f"slope {float(slope):.1f} deg"
    dist = r.distribution("slope_degrees")
    if dist:
        note = (f"slope {float(dist['best']):.1f}-{float(dist['worst']):.1f} deg, "
                f"{dist['fraction_usable'] * 100:.0f}% of sampled ground usable")
    return Component(_inverse_band(float(slope), th["slope_bands"]), note,
                     r.used, r.missing)


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



def _legacy(r: _Reader, th: dict) -> Component:
    """Legacy industrial infrastructure — the strongest real-world energy-park signal.

    Retired coal plants and brownfields are the sites the industry is actually
    converting, because they carry existing substations and transmission corridors,
    legacy interconnection position, historic water allocations, industrial zoning
    already in place, and materially less community pushback than greenfield.
    """
    fuel, _ = r.get("nearest_power_plant_primary_fuel")
    plant_d, _ = r.get("nearest_power_plant_distance_m")
    capacity, _ = r.get("nearest_power_plant_capacity_mw")
    repower, repower_present = r.get("near_epa_repowering_site")
    repower_d, _ = r.get("nearest_repowering_site_distance_m")
    brown_d, _ = r.get("nearest_brownfield_distance_m")

    sc, notes = th["legacy_base"], []

    if fuel and plant_d is not None:
        prox = _decay(float(plant_d), th["plant_full_m"], th["plant_zero_m"])
        heavy = str(fuel).lower() in th["heavy_interconnect_fuels"]
        big = capacity is not None and float(capacity) >= th["plant_capacity_mw_floor"]
        if heavy and prox > 0:
            sc = max(sc, th["legacy_base"] + (1 - th["legacy_base"]) * prox * (1.0 if big else 0.7))
            notes.append(
                f"{fuel} plant {float(plant_d)/1000:.1f} km"
                + (f", {float(capacity):.0f} MW" if capacity is not None else "")
            )

    if repower:
        sc = max(sc, th["repowering_onsite"])
        notes.append("EPA RE-Powering site")
    elif repower_d is not None:
        sc = max(sc, th["legacy_base"] + (th["repowering_onsite"] - th["legacy_base"])
                 * _decay(float(repower_d), th["repower_full_m"], th["repower_zero_m"]))
        notes.append(f"repowering site {float(repower_d)/1000:.1f} km")

    if brown_d is not None:
        sc = max(sc, th["legacy_base"] + (1 - th["legacy_base"])
                 * _decay(float(brown_d), th["brownfield_full_m"], th["brownfield_zero_m"]))
        notes.append(f"brownfield {float(brown_d)/1000:.1f} km")

    if not notes and not repower_present:
        return Component(0.5, "legacy infrastructure not fetched", r.used, r.missing)
    return Component(min(sc, 1.0), "; ".join(notes) or "no legacy industrial site nearby",
                     r.used, r.missing)


def _isolation(r: _Reader, th: dict) -> Component:
    """Distance from settlement. Higher isolation scores HIGHER.

    Inverted relative to every other component: an energy park wants to be away from
    people. `housing_units_within_1km` is Mireye's community-pushback exposure proxy.
    """
    density, present = r.get("housing_units_density_per_km2")
    urban_d, _ = r.get("nearest_urban_area_distance_m")
    if density is None and urban_d is None:
        return Component(0.5, "settlement context not fetched", r.used, r.missing)

    sc, notes = 1.0, []
    if density is not None:
        sc = _inverse_band(float(density), th["housing_density_bands"])
        notes.append(f"{float(density):.1f} homes/km2")
    if urban_d is not None:
        sc = min(1.0, sc * (th["urban_floor"] + (1 - th["urban_floor"])
                            * _band(float(urban_d), th["urban_distance_bands"])))
        notes.append(f"urban area {float(urban_d)/1000:.1f} km")
    return Component(sc, ", ".join(notes), r.used, r.missing)


def _low_disturbance(r: _Reader, th: dict) -> Component:
    """Prefer already-disturbed ground; penalise intact habitat and prime farmland.

    The goal is remote-but-not-pristine: away from people, without pushing an
    industrial footprint into forest, wetland or critical habitat.
    """
    land, present = r.get("land_use_class")
    canopy, _ = r.get("tree_canopy_pct")
    farmland, _ = r.get("prime_farmland_classification")
    habitat, _ = r.get("intersects_critical_habitat")

    if land is None and canopy is None:
        return Component(0.5, "land character not fetched", r.used, r.missing)

    sc, notes = 1.0, []
    if land is not None:
        key = str(land).lower()
        sc *= th["land_use_scores"].get(key, th["land_use_default"])
        notes.append(str(land))
    if canopy is not None:
        sc *= _inverse_band(float(canopy), th["canopy_bands"])
        notes.append(f"{float(canopy):.0f}% canopy")
    # Prime-farmland classification describes SOIL capability, not current use --
    # downtown Seattle reports prime farmland soil under pavement. Only penalise it
    # where the land is actually undeveloped and could still be farmed.
    developed = land is not None and str(land).lower() in th["developed_land_classes"]
    if farmland and "not prime" not in str(farmland).lower() and not developed:
        sc *= th["prime_farmland_penalty"]
        notes.append("prime farmland soil")
    if habitat:
        sc *= th["critical_habitat_penalty"]
        notes.append("critical habitat")
    return Component(max(sc, 0.0), ", ".join(notes), r.used, r.missing)


def _btm_fuel(r: _Reader, th: dict) -> Component:
    """Behind-the-meter fuel access.

    BTM generation is roughly 30% of planned US data-centre capacity, and gas turbines
    are ~75% of that, because grid interconnection in primary hubs now exceeds four
    years. Mireye ships `btm_gas_candidacy_flag` directly.
    """
    flag, present = r.get("btm_gas_candidacy_flag")
    pipe_d, _ = r.get("nearest_gas_pipeline_distance_m")
    cost, _ = r.get("modeled_onsite_gas_generation_cost_usd_per_mwh")

    if not present and pipe_d is None:
        return Component(0.5, "BTM fuel not fetched", r.used, r.missing)

    sc, notes = (th["btm_flagged"] if flag else th["btm_unflagged"]), []
    notes.append(f"btm_gas_candidate={flag}")
    if pipe_d is not None:
        sc *= (th["pipeline_floor"] + (1 - th["pipeline_floor"])
               * _decay(float(pipe_d), th["pipeline_full_m"], th["pipeline_zero_m"]))
        notes.append(f"gas pipeline {float(pipe_d)/1000:.1f} km")
    if cost is not None:
        sc *= _inverse_band(float(cost), th["onsite_gen_cost_bands"])
        notes.append(f"${float(cost):.0f}/MWh onsite")
    return Component(min(sc, 1.0), ", ".join(notes), r.used, r.missing)


def _fire_siting(r: _Reader, th: dict) -> Component:
    """BESS fire-siting penalty.

    NFPA 855 and IFC 1207.5.7 require combustible-vegetation clearance around
    pad-mounted BESS, and heavy canopy plus wildfire exposure raises siting cost and
    permitting friction. Applied as a penalty multiplier.
    """
    fire, present = r.get("wildfire_annual_frequency")
    canopy, _ = r.get("tree_canopy_pct")
    if fire is None and canopy is None:
        return Component(1.0, "fire exposure not fetched", r.used, r.missing)
    sc, notes = 1.0, []
    if fire is not None:
        sc *= _inverse_band(float(fire), th["wildfire_bands"])
        notes.append(f"wildfire freq {float(fire):.4f}")
    if canopy is not None and float(canopy) >= th["canopy_fire_threshold_pct"]:
        sc *= th["canopy_fire_penalty"]
        notes.append(f"{float(canopy):.0f}% canopy near BESS")
    return Component(sc, ", ".join(notes) or "no elevated fire exposure", r.used, r.missing)



def _latency(r: _Reader, th: dict) -> Component:
    """Network latency floor to the nearest urban population centre.

    A first-order data-centre variable that nothing else in this module captures:
    inference and interactive workloads are latency-bound, and the speed-of-light
    floor to users is irreducible once a site is chosen.
    """
    rtt, present = r.get("nearest_urban_area_rtt_floor_ms")
    urban_d, _ = r.get("nearest_urban_area_distance_m")
    if rtt is None:
        if urban_d is None:
            return Component(0.5, "latency not fetched", r.used, r.missing)
        return Component(_inverse_band(float(urban_d) / 1000, th["latency_km_bands"]),
                         f"urban area {float(urban_d)/1000:.0f} km (distance proxy)",
                         r.used, r.missing)
    return Component(_inverse_band(float(rtt), th["rtt_bands"]),
                     f"{float(rtt):.2f} ms RTT floor to urban area", r.used, r.missing)


def _carbon(r: _Reader, th: dict) -> Component:
    """Grid carbon intensity.

    Mireye: "an underwriting/ESG gate for 24/7-CFE data centers and a storage-arbitrage
    signal." Hyperscaler carbon commitments hard-gate siting, so a clean subregion is a
    real advantage, not a nicety.
    """
    co2, present = r.get("egrid_co2_output_rate_kg_per_mwh")
    if co2 is None:
        return Component(0.5, "grid carbon not fetched" if not present else "carbon absent",
                         r.used, r.missing)
    return Component(_inverse_band(float(co2), th["co2_bands"]),
                     f"{float(co2):.0f} kg CO2/MWh grid intensity", r.used, r.missing)


def _incentives(r: _Reader, th: dict) -> Component:
    """Tax and incentive stack. Data-centre siting is heavily incentive-driven."""
    stack, present = r.get("tax_incentive_stack")
    oz, _ = r.get("in_opportunity_zone")
    if stack is None and not oz:
        if not present:
            return Component(0.5, "incentives not fetched", r.used, r.missing)
        return Component(th["incentive_none"], "no incentive programmes mapped",
                         r.used, r.missing)
    items = [x.strip() for x in str(stack or "").replace(";", ",").split(",") if x.strip()]
    if oz and "opportunity_zone" not in items:
        items.append("opportunity_zone")
    sc = min(1.0, th["incentive_none"] + th["per_incentive"] * len(items))
    return Component(sc, ", ".join(items) or "none", r.used, r.missing)


COMPONENTS: dict[str, Callable[[_Reader, dict], Component]] = {
    "power": _power,
    "interconnect": _interconnect,
    "terrain": _terrain,
    "cooling": _cooling,
    "water": _water,
    "cost": _cost,
    "access": _access,
    "legacy": _legacy,
    "isolation": _isolation,
    "low_disturbance": _low_disturbance,
    "btm_fuel": _btm_fuel,
    "latency": _latency,
    "carbon": _carbon,
    "incentives": _incentives,
    "clear": _clear,
    "fire_siting": _fire_siting,
}

# ---------------------------------------------------------------------------
# thresholds -- every entry traceable to a Mireye interpretation_hint
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLDS: dict[str, Any] = {
    # ">=230 kV = transmission-grade, <100 kV = sub-transmission"
    "voltage_bands": [(230, 1.0), (100, 0.80), (1, 0.45)],
    "voltage_absent": 0.03,
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

    # --- energy park -------------------------------------------------------
    # Retired coal/gas plants carry substations, corridors and legacy queue position.
    "legacy_base": 0.35,
    "heavy_interconnect_fuels": ["coal", "natural gas", "gas", "petroleum", "nuclear"],
    "plant_capacity_mw_floor": 200,
    "plant_full_m": 3000,
    "plant_zero_m": 25000,
    "repowering_onsite": 0.95,
    "repower_full_m": 2000,
    "repower_zero_m": 20000,
    "brownfield_full_m": 2000,
    "brownfield_zero_m": 20000,
    # Isolation is INVERTED: fewer homes nearby scores higher.
    "housing_density_bands": [(2, 1.0), (10, 0.9), (50, 0.7), (200, 0.45), (1e9, 0.2)],
    "urban_distance_bands": [(20000, 1.0), (8000, 0.85), (3000, 0.6), (0, 0.35)],
    "urban_floor": 0.6,
    # Remote but not pristine: already-disturbed ground is preferred.
    "land_use_scores": {
        "developed": 1.0, "barren": 0.95, "shrubland": 0.85, "grassland": 0.8,
        "cropland": 0.7, "agriculture": 0.7, "forest": 0.45, "wetland": 0.15,
        "water": 0.05, "snow/ice": 0.1,
    },
    "land_use_default": 0.7,
    "canopy_bands": [(10, 1.0), (30, 0.85), (60, 0.6), (100, 0.4)],
    "prime_farmland_penalty": 0.7,
    "developed_land_classes": ["developed", "barren", "urban"],
    "critical_habitat_penalty": 0.2,
    # Behind-the-meter fuel.
    "btm_flagged": 1.0,
    "btm_unflagged": 0.45,
    "pipeline_full_m": 3000,
    "pipeline_zero_m": 30000,
    "pipeline_floor": 0.4,
    "onsite_gen_cost_bands": [(80, 1.0), (110, 0.9), (140, 0.75), (200, 0.5), (1e9, 0.3)],
    "osm_only_ceiling": 0.40,
    "rtt_bands": [(0.5, 1.0), (2, 0.92), (5, 0.8), (12, 0.6), (1e9, 0.4)],
    "latency_km_bands": [(15, 1.0), (50, 0.9), (150, 0.75), (400, 0.55), (1e9, 0.35)],
    "co2_bands": [(100, 1.0), (250, 0.9), (400, 0.75), (600, 0.55), (1e9, 0.4)],
    "incentive_none": 0.55,
    "per_incentive": 0.18,

    # --- gates -------------------------------------------------------------
    # PSD screening radius is customarily ~300 km; closer means heavier consultation.
    "class_i_bands": [(300, 1.0), (150, 0.85), (50, 0.6), (0, 0.4)],
    "gate_nonattainment": 0.55,
    "gate_maintenance": 0.85,
    "landslide_bands": [(20, 1.0), (50, 0.92), (75, 0.8), (90, 0.65), (100, 0.5)],
    "seismic_categories": {"A": 1.0, "B": 1.0, "C": 0.95, "D": 0.85, "E": 0.7, "F": 0.6},
    "karst_exposure": {"exposed": 0.55, "buried_under_thin_glacial_cover": 0.85,
                       "buried_under_insoluble_cover": 0.9, "unknown": 0.8},
    "karst_default": 0.8,
    # "Heavy active queue = constrained headroom + study-delay risk"
    "queue_bands": [(500, 1.0), (2000, 0.9), (5000, 0.78), (15000, 0.65), (1e9, 0.55)],

    # NFPA 855 / IFC 1207.5.7 combustible-vegetation clearance around pad-mounted BESS.
    "wildfire_bands": [(0.001, 1.0), (0.01, 0.9), (0.05, 0.7), (1e9, 0.5)],
    "canopy_fire_threshold_pct": 40,
    "canopy_fire_penalty": 0.85,
}

#: metric -> GRADED component weights (weighted geometric mean). PROVISIONAL.
DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    "data_center_optionality": {
        # power stays dominant deliberately: each component added dilutes every other
        # one under a geometric mean, and the no-grid veto is the guard that separates
        # this from a keyword feed. Do not shave this to make room for a new component.
        "power": 0.34, "interconnect": 0.14, "latency": 0.09, "terrain": 0.12,
        "cooling": 0.11, "water": 0.09, "carbon": 0.06, "cost": 0.05,
    },
    # A battery needs grid and ground. It does not need fibre, cooling water, or
    # evaporative-cooling climate -- it is a price-arbitrage asset, so cost matters.
    "bess_optionality": {"power": 0.60, "terrain": 0.25, "cost": 0.15},
    "buildability": {"terrain": 0.55, "access": 0.30, "water": 0.15},
    # Can this ground host a co-located data-centre + BESS energy park?
    # Encodes the observed industry pattern: legacy industrial land with inherited
    # grid infrastructure, away from settlement, on already-disturbed ground, with a
    # behind-the-meter fuel option to bridge multi-year interconnection queues.
    "energy_park_optionality": {
        "power": 0.22, "legacy": 0.18, "isolation": 0.13, "low_disturbance": 0.13,
        "btm_fuel": 0.13, "terrain": 0.09, "incentives": 0.12,
    },
}

#: metrics whose penalty set differs from the default
METRIC_PENALTIES: dict = {"energy_park_optionality": ("clear", "fire_siting")}

#: applied as a direct multiplier after the weighted mean, for every metric
PENALTY_COMPONENTS: tuple = ("clear",)

METRICS = tuple(DEFAULT_WEIGHTS)

_PER_COMPONENT = {
    "power": ["nearest_transmission_line_voltage_kv", "max_transmission_line_voltage_kv_within_radius",
              "nearest_transmission_line_voltage_class", "nearest_substation_distance_m",
              "nearest_osm_substation_distance_m", "nearest_osm_substation_max_voltage_kv"],
    "latency": ["nearest_urban_area_rtt_floor_ms", "nearest_urban_area_distance_m"],
    "carbon": ["egrid_co2_output_rate_kg_per_mwh"],
    "incentives": ["tax_incentive_stack", "in_opportunity_zone"],
    "interconnect": ["fiber_broadband_available", "fiber_provider_count",
                     "nearest_long_haul_rail_corridor_distance_m"],
    "terrain": ["slope_degrees"],
    "cooling": ["design_wet_bulb_temperature_0_4pct_degc", "days_above_32c_annual_count"],
    "water": ["within_water_service_area", "surface_water_supply_use_index_huc12"],
    "cost": ["avg_retail_electricity_price_industrial_usd_per_kwh"],
    "access": ["nearest_major_road_distance_m"],
    "clear": ["intersects_protected_area", "protected_area_gap_status", "protected_area_name",
              "within_floodplain_polygon", "intersects_wetland", "in_air_quality_nonattainment"],
    "legacy": ["near_epa_repowering_site", "nearest_repowering_site_distance_m",
               "nearest_brownfield_distance_m", "nearest_power_plant_distance_m",
               "nearest_power_plant_primary_fuel", "nearest_power_plant_capacity_mw"],
    "isolation": ["housing_units_density_per_km2", "nearest_urban_area_distance_m"],
    "low_disturbance": ["land_use_class", "tree_canopy_pct",
                        "prime_farmland_classification", "intersects_critical_habitat"],
    "btm_fuel": ["btm_gas_candidacy_flag", "nearest_gas_pipeline_distance_m",
                 "modeled_onsite_gas_generation_cost_usd_per_mwh"],
    "fire_siting": ["wildfire_annual_frequency", "tree_canopy_pct"],
}

#: qualifier fields a gate needs, fetched alongside the component it gates
_GATE_FIELDS = {
    "btm_fuel": ["nearest_class_i_area_distance_m", "nearest_class_i_area_name",
                 "in_air_quality_nonattainment", "in_air_quality_maintenance"],
    "terrain": ["landslide_susceptibility_index", "seismic_design_category",
                "in_karst_area", "karst_exposure_class"],
    "power": ["interconnection_queue_active_capacity_county_mw"],
}


def penalties_for(metric: str) -> tuple:
    return METRIC_PENALTIES.get(metric, PENALTY_COMPONENTS)


def required_fields(metric: str) -> list:
    out: list = []
    for component in list(DEFAULT_WEIGHTS[metric]) + list(penalties_for(metric)):
        for f in _PER_COMPONENT[component] + _GATE_FIELDS.get(component, []):
            if f not in out:
                out.append(f)
    return out


def all_feature_fields() -> list:
    """Every raw field any metric reads -- the column set for calibration export."""
    out: list = []
    for fields in list(_PER_COMPONENT.values()) + list(_GATE_FIELDS.values()):
        for f in fields:
            if f not in out:
                out.append(f)
    return out


def _apply_gates(component: str, dps: dict, th: dict, base: float):
    """Apply a component's qualifiers. Returns (gated_score, [gate records])."""
    applied: list = []
    score_out = base
    for gate_fn in GATES.get(component, []):
        r = _Reader(dps)  # gates read centroid facts; a gate is about THIS ground
        g = gate_fn(r, th)
        if g is None:
            continue
        score_out *= g.multiplier
        applied.append({"reason": g.reason, "multiplier": round(g.multiplier, 3),
                        "_fields": g.fields_used})
    return score_out, applied


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
    vicinity: Optional[dict] = None,
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
        r = _Reader(dps, vicinity)
        comp = COMPONENTS[name](r, th)
        gated, applied = _apply_gates(name, dps, th, comp.score)
        entry = {"score": round(gated, 4), "weight": round(weight / total, 4),
                 "basis": comp.basis}
        if applied:
            entry["ungated_score"] = round(comp.score, 4)
            entry["gates"] = applied
            for g in applied:
                used += g.pop("_fields", [])
        components[name] = entry
        used += comp.fields_used
        missing += comp.fields_missing
        product *= max(gated, 0.0) ** (weight / total)

    for name in penalties_for(metric):
        r = _Reader(dps, vicinity)
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
        "measurement": "vicinity" if vicinity else "point",
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
