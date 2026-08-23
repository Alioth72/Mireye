"""Bundle definitions.

Bundles are named after PHYSICAL SYSTEMS, not event types. There is no
``data_center_moratorium`` bundle, and that naming is the Phase 2 / Phase 3 boundary
made visible: choosing which bundle an event calls for is a materiality judgement and
belongs to Phase 3.
"""

from __future__ import annotations

from types import MappingProxyType

# ---------------------------------------------------------------------------
# The 19 members of Mireye's `parcel_record` metered group.
#
# 300 credits per location, charged once no matter how many members you request.
# Five of them do NOT look like parcel fields -- `wetland_acres_on_parcel`,
# `wetland_fraction_of_parcel`, `developable_acres_proxy`, and both
# `onsite_solar_potential_mwac_*` -- which is why membership is asserted by test
# rather than by eyeballing field names.
#
# The brief puts parcel/ownership fields out of scope entirely.
# ---------------------------------------------------------------------------
PARCEL_RECORD_GROUP: frozenset[str] = frozenset(
    {
        "parcel_id",
        "parcel_apn",
        "parcel_address",
        "parcel_area_m2",
        "parcel_owner",
        "parcel_zoning",
        "parcel_geometry_wkt",
        "parcel_boundary_geojson",
        "parcel_data_source",
        "parcel_match_type",
        "parcel_match_distance_m",
        "parcel_match_radius_m",
        "wetland_acres_on_parcel",
        "wetland_fraction_of_parcel",
        "easement_acres_on_parcel",
        "easement_fraction_of_parcel",
        "developable_acres_proxy",
        "onsite_solar_potential_mwac_low",
        "onsite_solar_potential_mwac_high",
    }
)

PARCEL_RECORD_CREDITS = 300


BUNDLES: MappingProxyType[str, tuple[str, ...]] = MappingProxyType(
    {
        # Power interconnect picture.
        "grid": (
            "nearest_transmission_line_voltage_kv",
            "nearest_transmission_line_distance_m",
            # Voltage CLASS/BASIS distinguish "no line in range" from "line, voltage
            # unpublished". Mireye: "Null here != 'no voltage' -- check voltage_class".
            "nearest_transmission_line_voltage_class",
            "nearest_substation_distance_m",
            # "may be LOWER than max within radius if a higher-voltage line runs
            # slightly farther; read both" -- so scoring takes the better of the two.
            "max_transmission_line_voltage_kv_within_radius",
            "transmission_redundancy_flag",
            "interconnection_queue_active_capacity_county_mw",
        ),
        # Fiber / connectivity.
        "telecom": (
            # NOTE: fiber_broadband_available is mass-market consumer FTTP and is an
            # availability FLOOR, not an interconnect signal (Mireye's own hint). The
            # long-haul corridor proxy is the closer signal for carrier backhaul.
            "fiber_broadband_available",
            "fiber_provider_count",
            "mobile_5g_coverage_class",
            "nearest_long_haul_rail_corridor_distance_m",
            "nearest_submarine_cable_distance_m",
        ),
        # Buildability of the ground.
        "terrain": (
            "slope_degrees",
            "elevation",
            "grading_difficulty_class",
        ),
        # Flood + wetland exposure.
        "water": (
            "within_floodplain_polygon",
            "fema_flood_zone",
            "intersects_wetland",
            "wetland_acres",
            # Cooling water. within_water_service_area is a mapped CWS boundary --
            # a strong sign municipal water exists, NOT a will-serve commitment.
            "within_water_service_area",
            "water_service_area_provenance",
            "surface_water_supply_use_index_huc12",
            "huc12_thermoelectric_consumptive_use_m3_per_day",
        ),
        # Protected / habitat / easement. These live in the `parcels` LAYER but are
        # NOT members of the metered `parcel_record` GROUP -- 1 credit each.
        "constraints": (
            "intersects_protected_area",
            # GAP status is what makes the protected flag meaningful. Mireye: "1/2 =
            # real conservation protection ... 4 ~ nominal -- Do NOT overstate GAP 4
            # as a development constraint." Without this, a city park reads as a
            # hard disqualifier.
            "protected_area_gap_status",
            "protected_area_designation",
            "protected_area_name",
            "intersects_critical_habitat",
            "intersects_conservation_easement",
        ),
        # Roads and rail.
        "access": (
            "nearest_major_road_distance_m",
            "nearest_major_road_class",
            "nearest_rail_line_distance_m",
        ),
        # Nonattainment triggers stricter New Source Review / emission offsets for
        # combustion equipment -- directly raises permitting cost and timeline for
        # backup diesel and on-site gas at a data centre.
        "airquality": (
            "in_air_quality_nonattainment",
            "air_quality_nonattainment_pollutants",
            "air_quality_worst_classification",
        ),
        # Evaporative-cooling viability. design_wet_bulb is "THE variable": >~25C
        # means low evap potential, oversized towers, less water saving.
        "climate": (
            "design_wet_bulb_temperature_0_4pct_degc",
            "days_above_32c_annual_count",
            "mean_annual_relative_humidity_pct",
        ),
        # Operating cost and community-pushback exposure.
        "market": (
            "avg_retail_electricity_price_industrial_usd_per_kwh",
            "housing_units_within_1km",
            "housing_units_density_per_km2",
            "nearest_urban_area_distance_m",
        ),
        # Legacy industrial infrastructure -- the strongest real-world signal for a
        # data-centre + BESS energy park. Retired coal plants and brownfields carry
        # existing substations, transmission corridors, legacy interconnection queue
        # position, historic water allocations and industrial zoning, with less
        # community pushback than greenfield. (PNNL coal-to-data-centre; EPA reuse
        # considerations; FAS adaptive reuse of legacy coal.)
        "energy_park": (
            "near_epa_repowering_site",
            "nearest_repowering_site_distance_m",
            "nearest_brownfield_distance_m",
            "nearest_power_plant_distance_m",
            "nearest_power_plant_primary_fuel",
            "nearest_power_plant_capacity_mw",
            "transmission_redundancy_flag",
        ),
        # Behind-the-meter fuel. BTM generation is ~30% of planned US data-centre
        # capacity and gas turbines are ~75% of it, because grid interconnection in
        # primary hubs now exceeds four years. Mireye ships a literal candidacy flag.
        "btm_fuel": (
            "btm_gas_candidacy_flag",
            "nearest_gas_pipeline_distance_m",
            "nearest_interstate_gas_pipeline_distance_m",
            "modeled_onsite_gas_generation_cost_usd_per_mwh",
            "natural_gas_industrial_price_usd_per_mcf",
        ),
        # Land character: is this already-disturbed industrial ground, or intact
        # habitat? Also drives BESS fire siting -- NFPA 855 / IFC 1207.5.7 require
        # combustible-vegetation clearance around pad-mounted units.
        "land_character": (
            "land_use_class",
            "tree_canopy_pct",
            "prime_farmland_classification",
            "wildfire_annual_frequency",
        ),
        # Pulled once at site registration. Phase 3 uses these for scope resolution.
        "boundaries": (
            "political_region",
            "political_county",
            "political_locality",
            "tract_geoid",
        ),
    }
)

# Mireye caps a fetch at 50 fields after preset expansion.
MAX_FIELDS_PER_FETCH = 50


class UnknownBundle(KeyError):
    pass


def bundle_fields(name: str) -> tuple[str, ...]:
    try:
        return BUNDLES[name]
    except KeyError as exc:
        raise UnknownBundle(
            f"unknown bundle {name!r}; known bundles: {', '.join(sorted(BUNDLES))}"
        ) from exc


def fields_for(names: list[str] | tuple[str, ...]) -> list[str]:
    """Union the fields of several bundles, preserving order and de-duplicating."""
    seen: dict[str, None] = {}
    for name in names:
        for field in bundle_fields(name):
            seen.setdefault(field, None)
    return list(seen)


def touches_parcel_record(fields: list[str] | tuple[str, ...]) -> set[str]:
    """Return any requested fields that would drag in the 300-credit parcel record."""
    return PARCEL_RECORD_GROUP.intersection(fields)


def estimate_credits(fields: list[str] | tuple[str, ...]) -> int:
    """Local estimate. The authority is always /v1/fetch/quote -- this exists so a
    caller can sanity-check before spending, not to replace the quote."""
    unique = set(fields)
    parcel_members = PARCEL_RECORD_GROUP.intersection(unique)
    non_parcel = unique - parcel_members
    total = len(non_parcel)
    if parcel_members:
        total += PARCEL_RECORD_CREDITS
    return total
