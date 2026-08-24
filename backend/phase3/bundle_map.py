"""(event_type, subject) -> Phase 2 bundles, and -> which derived optionality metric.

Phase 2 publishes the recommendation table (context/phase2.md sec. "Recommended mapping");
choosing which bundle an event calls for is a materiality judgement and is explicitly
Phase 3's to own and apply.

`subject` is LLM-written free text (e.g. "data centers", "industrial zoning in SODO"), not
an enum -- there is no guarantee it matches a fixed vocabulary. Classification here is
keyword-based and always falls back to the SAFE SUPERSET when ambiguous or unrecognized,
never to the narrower/cheaper bundle -- guessing toward the cheaper selection risks missing
the field that would have made an event material.
"""

from __future__ import annotations

from monitor_records.models import EventType

_DATA_CENTER_KW = {"data center", "datacenter", "data centre", "hyperscale", "server farm"}
_BESS_KW = {"bess", "battery", "battery storage", "energy storage"}
_POWER_KW = {"power", "electric", "transmission", "substation", "grid"}
_WATER_KW = {"sewer", "water main", "stormwater", "water line", "wastewater"}


def bundles_for(event_type: EventType, subject: str | None) -> list[str]:
    s = (subject or "").casefold()

    if event_type == EventType.MORATORIUM:
        is_bess = any(k in s for k in _BESS_KW)
        is_dc = any(k in s for k in _DATA_CENTER_KW)
        if is_bess and not is_dc:
            return ["grid", "terrain", "water", "constraints"]  # 16 credits, no fiber
        return ["grid", "telecom", "terrain", "water", "constraints"]  # 19 credits; default/DC/ambiguous

    if event_type in (
        EventType.REZONING,
        EventType.ANNEXATION,
        EventType.COMP_PLAN_AMENDMENT,
        EventType.MAJOR_DEVELOPMENT_PERMIT,
    ):
        return ["terrain", "water", "constraints", "access"]

    if event_type == EventType.UTILITY_EXTENSION:
        is_power = any(k in s for k in _POWER_KW)
        is_water = any(k in s for k in _WATER_KW)
        if is_power and not is_water:
            return ["grid", "terrain", "access"]
        if is_water and not is_power:
            return ["terrain", "water", "access"]
        return ["grid", "terrain", "water", "access"]  # ambiguous/unspecified: fetch the union

    return ["terrain", "water", "constraints"]  # unknown/future event_type: generic default


def metric_for(event_type: EventType, subject: str | None) -> str:
    """Which derived optionality score answers "does this event materially change this
    site's options" -- a capability measure, evaluated once the stage/geography gates
    have already passed."""
    s = (subject or "").casefold()
    if event_type == EventType.MORATORIUM and any(k in s for k in _BESS_KW) and not any(
        k in s for k in _DATA_CENTER_KW
    ):
        return "bess_optionality"
    if event_type == EventType.MORATORIUM:
        return "data_center_optionality"
    return "buildability"
