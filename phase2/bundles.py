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
            "nearest_substation_distance_m",
            "max_transmission_line_voltage_kv_within_radius",
            "transmission_redundancy_flag",
            "interconnection_queue_active_capacity_county_mw",
        ),
        # Fiber / connectivity.
        "telecom": (
            "fiber_broadband_available",
            "fiber_provider_count",
            "mobile_5g_coverage_class",
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
        ),
        # Protected / habitat / easement. These live in the `parcels` LAYER but are
        # NOT members of the metered `parcel_record` GROUP -- 1 credit each.
        "constraints": (
            "intersects_protected_area",
            "intersects_critical_habitat",
            "intersects_conservation_easement",
        ),
        # Roads and rail.
        "access": (
            "nearest_major_road_distance_m",
            "nearest_major_road_class",
            "nearest_rail_line_distance_m",
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
