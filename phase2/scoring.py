"""Derived optionality scores -- pure functions of physical fact, no event input.

Per the ideation doc (Plan/phase2-mireye-backend-ideation.md sec. 5.4): "No field is called
`had_data_center_optionality` -- derive it." These are *capability* measures ("could this
ground host a data center"), never materiality -- that question needs the event and belongs
to Phase 3 (``phase3/pipeline.py``).

The composite is **multiplicative, not additive**. A site with no power has zero data-center
optionality no matter how flat it is; additive weights would score flat unpowered farmland as
a near-miss, which is exactly the "keyword feed" failure mode the brief warns against. An
override profile may change weights and thresholds; it may not change the composition rule.

``absent`` vs ``failed`` matters most in ``_clear_component``: a constraint field reporting
``absent`` is positive evidence of clearance (the source affirmatively found nothing there)
and scores as fully clear; ``failed`` is missing data and must never be read as either "clear"
or "flagged" -- it scores as a mild, explicit uncertainty penalty instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

FieldMap = dict[str, dict]  # field_name -> phase2.store.serialize(dp) shape

UNCERTAIN = 0.4  # failed / missing data: never treated as "clear" nor as "flagged"


@dataclass
class Component:
    score: float  # 0..1
    weight: float  # exponent in the geometric-mean composite
    basis: str  # human-readable, cites the actual field values -- this is the evidence trail


@dataclass
class OptionalityScore:
    metric: str
    profile: str
    score: float
    components: dict[str, Component] = field(default_factory=dict)
    fields_used: list[str] = field(default_factory=list)
    fields_missing: list[str] = field(default_factory=list)


def _status_value(fields: FieldMap, name: str) -> tuple[str | None, object]:
    row = fields.get(name)
    if row is None:
        return None, None
    return row.get("status"), row.get("value")


def _power_component(fields: FieldMap) -> Component:
    status, kv = _status_value(fields, "nearest_transmission_line_voltage_kv")
    _, dist_m = _status_value(fields, "nearest_substation_distance_m")

    if status == "failed" or status is None:
        return Component(UNCERTAIN, 0.0, "transmission voltage data unavailable (failed/missing fetch)")
    if status == "absent" or kv is None:
        return Component(0.1, 0.0, "no transmission line found near this site")

    if kv >= 230:
        band, band_desc = 1.0, "extra-high voltage (>=230 kV)"
    elif kv >= 115:
        band, band_desc = 0.75, "high voltage (115-230 kV)"
    elif kv >= 69:
        band, band_desc = 0.45, "subtransmission voltage (69-115 kV)"
    else:
        band, band_desc = 0.15, "distribution-only voltage (<69 kV)"

    if isinstance(dist_m, (int, float)):
        if dist_m <= 5_000:
            atten, atten_desc = 1.0, f"substation {dist_m:.0f} m away"
        elif dist_m <= 15_000:
            atten, atten_desc = 0.85, f"substation {dist_m:.0f} m away"
        elif dist_m <= 30_000:
            atten, atten_desc = 0.6, f"substation {dist_m:.0f} m away"
        else:
            atten, atten_desc = 0.4, f"substation {dist_m:.0f} m away"
    else:
        atten, atten_desc = 0.7, "substation distance unknown"

    return Component(round(band * atten, 3), 0.0, f"{kv:.0f} kV at nearest line, {atten_desc} ({band_desc})")


def _fiber_component(fields: FieldMap) -> Component:
    status, available = _status_value(fields, "fiber_broadband_available")
    if status == "failed" or status is None:
        return Component(UNCERTAIN, 0.0, "fiber availability data unavailable (failed/missing fetch)")
    if status == "absent":
        return Component(0.2, 0.0, "no fiber provider record found for this site")
    if available:
        return Component(1.0, 0.0, "fiber_broadband_available = true")
    return Component(0.05, 0.0, "fiber_broadband_available = false")


def _terrain_component(fields: FieldMap) -> Component:
    status, slope = _status_value(fields, "slope_degrees")
    if status in (None, "failed") or slope is None:
        return Component(UNCERTAIN, 0.0, "slope data unavailable (failed/missing fetch)")
    if slope < 5:
        return Component(1.0, 0.0, f"slope {slope:.1f} deg (flat)")
    if slope < 10:
        return Component(0.7, 0.0, f"slope {slope:.1f} deg (moderate)")
    if slope < 15:
        return Component(0.4, 0.0, f"slope {slope:.1f} deg (steep)")
    return Component(0.1, 0.0, f"slope {slope:.1f} deg (very steep)")


_CLEAR_FLAG_FIELDS = (
    "within_floodplain_polygon",
    "intersects_wetland",
    "intersects_protected_area",
    "intersects_critical_habitat",
    "intersects_conservation_easement",
)


def _clear_component(fields: FieldMap) -> Component:
    """Worst-flag-wins across the constraint fields. `absent` on a flag is affirmative
    evidence of clearance (score 1.0); `ok`+True is a real hit (score ~0.05); `failed` or
    missing is uncertainty, never silently treated as either clear or flagged."""
    worst_score = 1.0
    worst_basis = "no constraint flags on record"
    seen_any = False
    for name in _CLEAR_FLAG_FIELDS:
        status, value = _status_value(fields, name)
        if status is None:
            continue
        seen_any = True
        if status == "absent":
            this_score, this_basis = 1.0, f"{name}: absent (confirmed clear)"
        elif status == "failed":
            this_score, this_basis = UNCERTAIN, f"{name}: unavailable (failed fetch)"
        elif value:
            this_score, this_basis = 0.05, f"{name}: true (flagged)"
        else:
            this_score, this_basis = 1.0, f"{name}: false (confirmed clear)"
        if this_score < worst_score:
            worst_score, worst_basis = this_score, this_basis
    if not seen_any:
        return Component(UNCERTAIN, 0.0, "no constraint data available (failed/missing fetch)")
    return Component(worst_score, 0.0, worst_basis)


# metric -> [(component_fn, weight), ...]. `fiber` is absent entirely from bess_optionality
# per the ideation doc -- fiber is decisive for data centers and noise for battery storage.
_METRIC_COMPONENTS: dict[str, list[tuple[Callable[[FieldMap], Component], float]]] = {
    "data_center_optionality": [
        (_power_component, 0.4),
        (_fiber_component, 0.2),
        (_terrain_component, 0.15),
        (_clear_component, 1.0),
    ],
    "bess_optionality": [
        (_power_component, 0.5),
        (_terrain_component, 0.2),
        (_clear_component, 1.0),
    ],
    "buildability": [
        (_terrain_component, 0.3),
        (_clear_component, 1.0),
    ],
}


def score_metric(
    metric: str,
    fields: FieldMap,
    *,
    weights: dict[str, float] | None = None,
    profile: str = "default",
) -> OptionalityScore:
    """Compute a derived optionality score from a bundle of serialized datapoints.

    `fields` is `{field_name: phase2.store.serialize(dp)}` for whatever bundle(s) were
    fetched. `weights` optionally overrides the per-component exponents (the shape stored
    on `ScoreProfile.weights`) -- it cannot change which components apply to a metric or
    the multiplicative composition rule itself, only how much each component counts.
    """
    if metric not in _METRIC_COMPONENTS:
        raise KeyError(f"unknown metric {metric!r}; known: {', '.join(sorted(_METRIC_COMPONENTS))}")

    composite = 1.0
    components: dict[str, Component] = {}
    fields_used: list[str] = []
    fields_missing: list[str] = []

    for fn, default_weight in _METRIC_COMPONENTS[metric]:
        name = fn.__name__.strip("_").removesuffix("_component")
        w = (weights or {}).get(name, default_weight)
        c = fn(fields)
        c.weight = w
        composite *= c.score ** w
        components[name] = c

    for name, row in fields.items():
        (fields_used if row.get("status") in ("ok", "absent") else fields_missing).append(name)

    return OptionalityScore(
        metric=metric,
        profile=profile,
        score=round(composite, 3),
        components=components,
        fields_used=sorted(fields_used),
        fields_missing=sorted(fields_missing),
    )
