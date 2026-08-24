from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .models import MireyeField


ALIASES: dict[str, tuple[str, ...]] = {
    "nearest_transmission_line_voltage_kv": (
        "nearest_transmission_line_voltage_kv",
        "nearest_line_voltage_kv",
        "nearest_transmission_voltage_kv",
    ),
    "nearest_transmission_line_distance_m": (
        "nearest_transmission_line_distance_m",
        "nearest_line_distance_m",
        "nearest_transmission_distance_m",
    ),
    "nearest_substation_distance_m": (
        "nearest_substation_distance_m",
        "substation_distance_m",
    ),
    "fiber_broadband_available": (
        "fiber_broadband_available",
        "fiber_available",
        "broadband_available",
    ),
    "slope_degrees": ("slope_degrees", "slope"),
    "within_floodplain_polygon": (
        "within_floodplain_polygon",
        "within_floodplain",
        "fema_floodplain",
    ),
    "intersects_wetland": ("intersects_wetland", "wetland_intersection"),
    "intersects_protected_area": (
        "intersects_protected_area",
        "intersects_conservation_easement",
        "protected_area_intersection",
    ),
    "nearest_major_road_distance_m": (
        "nearest_major_road_distance_m",
        "nearest_road_distance_m",
    ),
    "water_system_distance_m": (
        "nearest_water_system_distance_m",
        "water_system_distance_m",
    ),
}


def field(fields: dict[str, MireyeField], canonical_name: str) -> MireyeField | None:
    for name in ALIASES.get(canonical_name, (canonical_name,)):
        if name in fields:
            return fields[name]
    return None


def value(fields: dict[str, MireyeField], canonical_name: str, default: Any = None) -> Any:
    found = field(fields, canonical_name)
    if found is None or found.status == "failed":
        return default
    if found.status == "absent" and _is_boolean_field(canonical_name):
        return False
    return found.value


def as_float(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def as_bool(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"true", "yes", "y", "1", "inside", "intersects"}:
            return True
        if normalized in {"false", "no", "n", "0", "outside", "none"}:
            return False
    return None


def missing(fields: dict[str, MireyeField], names: Iterable[str]) -> list[str]:
    return [name for name in names if field(fields, name) is None or field(fields, name).status == "failed"]


def citation_for(field_obj: MireyeField | None) -> dict[str, Any] | None:
    if field_obj is None:
        return None
    citation = {"field": field_obj.name, "value": field_obj.value}
    if field_obj.unit:
        citation["unit"] = field_obj.unit
    if field_obj.source:
        citation["source"] = field_obj.source
    if field_obj.source_url:
        citation["source_url"] = field_obj.source_url
    citation["status"] = field_obj.status
    if field_obj.license:
        citation["license"] = field_obj.license
    if field_obj.confidence is not None:
        citation["confidence"] = field_obj.confidence
    if field_obj.fetched_at:
        citation["fetched_at"] = field_obj.fetched_at
    if field_obj.stale:
        citation["stale"] = field_obj.stale
    if field_obj.profile:
        citation["profile"] = field_obj.profile
    if field_obj.notes:
        citation["notes"] = field_obj.notes
    return citation


def _is_boolean_field(name: str) -> bool:
    return name.startswith(("within_", "intersects_")) or name.endswith("_available") or name.endswith("_flag")
