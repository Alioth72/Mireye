from __future__ import annotations

from statistics import mean
from typing import Any

from .fields import as_bool, as_float, citation_for, field, missing, value
from .models import (
    AlertDecision,
    DecisionRequest,
    DecisionResponse,
    EventStage,
    EventType,
    ImpactDirection,
    ScoreBreakdown,
)


EVENT_RELEVANCE = {
    EventType.DATA_CENTER_MORATORIUM: 1.00,
    EventType.BESS_MORATORIUM: 0.95,
    EventType.UTILITY_EXTENSION: 0.90,
    EventType.REZONING: 0.82,
    EventType.COMP_PLAN_AMENDMENT: 0.76,
    EventType.ANNEXATION: 0.70,
    EventType.BIG_PERMIT: 0.62,
    EventType.UNKNOWN: 0.35,
}

STAGE_WEIGHT = {
    EventStage.PROPOSED: 0.72,
    EventStage.HEARD: 0.86,
    EventStage.ADOPTED: 1.00,
    EventStage.REJECTED: 0.95,
    EventStage.WITHDRAWN: 0.18,
    EventStage.TABLED: 0.35,
    EventStage.UNKNOWN: 0.50,
}

CORE_FIELDS_BY_EVENT = {
    EventType.DATA_CENTER_MORATORIUM: [
        "nearest_transmission_line_voltage_kv",
        "nearest_transmission_line_distance_m",
        "nearest_substation_distance_m",
        "fiber_broadband_available",
        "slope_degrees",
        "within_floodplain_polygon",
        "intersects_wetland",
        "intersects_protected_area",
    ],
    EventType.BESS_MORATORIUM: [
        "nearest_transmission_line_voltage_kv",
        "nearest_transmission_line_distance_m",
        "nearest_substation_distance_m",
        "slope_degrees",
        "within_floodplain_polygon",
        "intersects_wetland",
        "intersects_protected_area",
    ],
    EventType.UTILITY_EXTENSION: [
        "slope_degrees",
        "within_floodplain_polygon",
        "intersects_wetland",
        "intersects_protected_area",
        "nearest_major_road_distance_m",
    ],
    EventType.REZONING: [
        "slope_degrees",
        "within_floodplain_polygon",
        "intersects_wetland",
        "intersects_protected_area",
        "nearest_major_road_distance_m",
    ],
    EventType.COMP_PLAN_AMENDMENT: [
        "slope_degrees",
        "within_floodplain_polygon",
        "intersects_wetland",
        "intersects_protected_area",
    ],
    EventType.ANNEXATION: [
        "slope_degrees",
        "within_floodplain_polygon",
        "intersects_wetland",
        "intersects_protected_area",
    ],
    EventType.BIG_PERMIT: ["nearest_major_road_distance_m"],
}


def score_decision(request: DecisionRequest) -> DecisionResponse:
    event = request.event
    required_fields = CORE_FIELDS_BY_EVENT.get(event.type, CORE_FIELDS_BY_EVENT[EventType.COMP_PLAN_AMENDMENT])
    missing_fields = missing(request.fields, required_fields)

    physical_score, physical_reasons, physical_citations = _physical_optionality(event.type, request.fields)
    scope_fit, scope_reason = _scope_fit(event.scope.relation_to_site, event.scope.distance_m)
    stage_weight = STAGE_WEIGHT[event.stage]
    event_relevance = EVENT_RELEVANCE[event.type]
    evidence_confidence = _evidence_confidence(request.fields, required_fields, event.source_url, event.source_quote)

    materiality = round(100 * event_relevance * stage_weight * scope_fit * physical_score * evidence_confidence)
    impact_direction = _impact_direction(event.type, event.scope.relation_to_site, event.stage)
    decision = _decision_for(materiality, missing_fields, event.stage, physical_score)
    confidence = _confidence_label(evidence_confidence, missing_fields, event.stage)

    event_citation = {
        "field": "public_record_event",
        "value": event.title,
        "stage": event.stage.value,
        "jurisdiction": event.jurisdiction,
    }
    if event.source_url:
        event_citation["source_url"] = event.source_url
    if event.source_quote:
        event_citation["quote"] = event.source_quote

    rationale = [
        f"Event relevance is {event_relevance:.2f} for {event.type.value}.",
        f"Stage is {event.stage.value}; scoring treats this as a {stage_weight:.2f} weight, not a done deal unless adopted.",
        scope_reason,
        *physical_reasons,
    ]

    quiet_reason = None
    if decision == AlertDecision.QUIET:
        quiet_reason = _quiet_reason(materiality, physical_score, scope_fit, missing_fields)

    return DecisionResponse(
        decision=decision,
        materiality_score=materiality,
        confidence=confidence,
        impact_direction=impact_direction,
        event_stage=event.stage,
        headline=_headline(decision, event, materiality, impact_direction),
        rationale=rationale,
        quiet_reason=quiet_reason,
        missing_fields=missing_fields,
        required_fields=required_fields,
        score_breakdown=ScoreBreakdown(
            event_relevance=round(event_relevance, 3),
            stage_weight=round(stage_weight, 3),
            scope_fit=round(scope_fit, 3),
            physical_optionality=round(physical_score, 3),
            evidence_confidence=round(evidence_confidence, 3),
        ),
        citations=[event_citation, *physical_citations],
        next_best_action=_next_best_action(decision, missing_fields, event.type),
    )


def _physical_optionality(event_type: EventType, fields: dict[str, Any]) -> tuple[float, list[str], list[dict[str, Any]]]:
    if event_type in {EventType.DATA_CENTER_MORATORIUM, EventType.BESS_MORATORIUM}:
        return _grid_optionality(fields, include_fiber=event_type == EventType.DATA_CENTER_MORATORIUM)
    if event_type in {
        EventType.REZONING,
        EventType.COMP_PLAN_AMENDMENT,
        EventType.ANNEXATION,
        EventType.UTILITY_EXTENSION,
    }:
        return _developability_optionality(fields)
    if event_type == EventType.BIG_PERMIT:
        return _nearby_permit_optionality(fields)
    return 0.35, ["Unknown event type; conservative materiality score applied."], []


def _grid_optionality(fields: dict[str, Any], include_fiber: bool) -> tuple[float, list[str], list[dict[str, Any]]]:
    voltage = as_float(value(fields, "nearest_transmission_line_voltage_kv"))
    line_distance = as_float(value(fields, "nearest_transmission_line_distance_m"))
    substation_distance = as_float(value(fields, "nearest_substation_distance_m"))
    fiber = as_bool(value(fields, "fiber_broadband_available"))
    slope = as_float(value(fields, "slope_degrees"))
    floodplain = as_bool(value(fields, "within_floodplain_polygon"))
    wetland = as_bool(value(fields, "intersects_wetland"))
    protected = as_bool(value(fields, "intersects_protected_area"))

    scores = {
        "grid_voltage": _threshold_score(voltage, [(345, 1.0), (230, 0.9), (115, 0.65), (69, 0.35)], default=0.25),
        "line_distance": _inverse_distance_score(line_distance, [(1_500, 1.0), (5_000, 0.8), (10_000, 0.45), (20_000, 0.2)]),
        "substation_distance": _inverse_distance_score(substation_distance, [(3_000, 1.0), (8_000, 0.72), (15_000, 0.38), (30_000, 0.15)]),
        "fiber": 1.0 if fiber is True else 0.45 if fiber is None or not include_fiber else 0.15,
        "slope": _inverse_distance_score(slope, [(5, 1.0), (10, 0.78), (15, 0.45), (25, 0.12)]),
        "floodplain": 0.12 if floodplain is True else 0.85 if floodplain is None else 1.0,
        "wetland": 0.10 if wetland is True else 0.85 if wetland is None else 1.0,
        "protected": 0.05 if protected is True else 0.90 if protected is None else 1.0,
    }
    weights = {
        "grid_voltage": 0.20,
        "line_distance": 0.19,
        "substation_distance": 0.17,
        "fiber": 0.10 if include_fiber else 0.03,
        "slope": 0.12,
        "floodplain": 0.08,
        "wetland": 0.07,
        "protected": 0.07,
    }
    total_weight = sum(weights.values())
    score = sum(scores[key] * weight for key, weight in weights.items()) / total_weight

    reasons = [
        f"Grid optionality score is {score:.2f}: voltage={_fmt(voltage, 'kV')}, line_distance={_fmt(line_distance, 'm')}, substation_distance={_fmt(substation_distance, 'm')}.",
        f"Physical constraints: slope={_fmt(slope, 'deg')}, floodplain={floodplain}, wetland={wetland}, protected_area={protected}.",
    ]
    if include_fiber:
        reasons.append(f"Fiber availability is {fiber}; this matters for data-center optionality.")

    citations = _citations(
        fields,
        [
            "nearest_transmission_line_voltage_kv",
            "nearest_transmission_line_distance_m",
            "nearest_substation_distance_m",
            "fiber_broadband_available",
            "slope_degrees",
            "within_floodplain_polygon",
            "intersects_wetland",
            "intersects_protected_area",
        ],
    )
    return _clamp(score), reasons, citations


def _developability_optionality(fields: dict[str, Any]) -> tuple[float, list[str], list[dict[str, Any]]]:
    slope = as_float(value(fields, "slope_degrees"))
    floodplain = as_bool(value(fields, "within_floodplain_polygon"))
    wetland = as_bool(value(fields, "intersects_wetland"))
    protected = as_bool(value(fields, "intersects_protected_area"))
    road_distance = as_float(value(fields, "nearest_major_road_distance_m"))

    scores = [
        _inverse_distance_score(slope, [(5, 1.0), (10, 0.80), (15, 0.50), (25, 0.18)]),
        0.18 if floodplain is True else 0.85 if floodplain is None else 1.0,
        0.16 if wetland is True else 0.85 if wetland is None else 1.0,
        0.08 if protected is True else 0.90 if protected is None else 1.0,
        _inverse_distance_score(road_distance, [(500, 1.0), (2_000, 0.80), (5_000, 0.45), (10_000, 0.20)]),
    ]
    score = mean(scores)
    reasons = [
        f"Developability score is {score:.2f}: slope={_fmt(slope, 'deg')}, road_distance={_fmt(road_distance, 'm')}.",
        f"Constraint flags: floodplain={floodplain}, wetland={wetland}, protected_area={protected}.",
    ]
    citations = _citations(
        fields,
        [
            "slope_degrees",
            "within_floodplain_polygon",
            "intersects_wetland",
            "intersects_protected_area",
            "nearest_major_road_distance_m",
        ],
    )
    return _clamp(score), reasons, citations


def _nearby_permit_optionality(fields: dict[str, Any]) -> tuple[float, list[str], list[dict[str, Any]]]:
    road_distance = as_float(value(fields, "nearest_major_road_distance_m"))
    score = _inverse_distance_score(road_distance, [(500, 1.0), (2_000, 0.75), (5_000, 0.42), (10_000, 0.18)])
    return score, [f"Nearby permit score is {score:.2f}; road_distance={_fmt(road_distance, 'm')}."], _citations(
        fields, ["nearest_major_road_distance_m"]
    )


def _scope_fit(relation_to_site: str, distance_m: float | None) -> tuple[float, str]:
    relation = relation_to_site.lower()
    if relation in {"inside", "intersects", "same_jurisdiction", "direct"}:
        return 1.0, f"Scope fit is direct: relation_to_site={relation}."
    if relation in {"adjacent", "neighboring_jurisdiction"}:
        return 0.88, f"Scope fit is adjacent, so materiality is discounted but not ignored."
    if relation in {"nearby", "buffer"}:
        score = _inverse_distance_score(distance_m, [(1_000, 0.86), (5_000, 0.62), (15_000, 0.32), (40_000, 0.10)])
        return score, f"Scope fit is distance-based: distance_m={_fmt(distance_m, 'm')}."
    if distance_m is not None:
        score = _inverse_distance_score(distance_m, [(1_000, 0.78), (5_000, 0.50), (15_000, 0.22), (40_000, 0.08)])
        return score, f"Scope relation is unknown; distance_m={_fmt(distance_m, 'm')} drives a conservative scope score."
    return 0.42, "Scope relation is unknown; conservative score applied until geometry is resolved."


def _evidence_confidence(
    fields: dict[str, Any], required_fields: list[str], source_url: str | None, source_quote: str | None
) -> float:
    present_ratio = 1 - (len(missing(fields, required_fields)) / max(len(required_fields), 1))
    public_record_score = 1.0 if source_url and source_quote else 0.78 if source_url else 0.60
    return _clamp((0.72 * present_ratio) + (0.28 * public_record_score), 0.25, 1.0)


def _decision_for(materiality: int, missing_fields: list[str], stage: EventStage, physical_score: float) -> AlertDecision:
    if stage in {EventStage.WITHDRAWN, EventStage.TABLED}:
        return AlertDecision.REVIEW if materiality >= 30 or missing_fields else AlertDecision.QUIET
    if materiality >= 62 and not missing_fields:
        return AlertDecision.ALERT
    if materiality >= 42 or physical_score >= 0.70 or stage == EventStage.UNKNOWN or _too_many_missing(missing_fields):
        return AlertDecision.REVIEW
    return AlertDecision.QUIET


def _impact_direction(event_type: EventType, relation_to_site: str, stage: EventStage) -> ImpactDirection:
    if event_type in {EventType.DATA_CENTER_MORATORIUM, EventType.BESS_MORATORIUM}:
        if stage == EventStage.REJECTED:
            return ImpactDirection.POSITIVE
        if stage in {EventStage.WITHDRAWN, EventStage.TABLED}:
            return ImpactDirection.MIXED
        if relation_to_site in {"adjacent", "neighboring_jurisdiction"}:
            return ImpactDirection.POSITIVE
        return ImpactDirection.NEGATIVE
    if event_type in {EventType.UTILITY_EXTENSION, EventType.REZONING, EventType.ANNEXATION, EventType.COMP_PLAN_AMENDMENT}:
        return ImpactDirection.POSITIVE
    return ImpactDirection.UNKNOWN


def _confidence_label(evidence_confidence: float, missing_fields: list[str], stage: EventStage) -> str:
    if evidence_confidence >= 0.84 and not missing_fields and stage != EventStage.UNKNOWN:
        return "high"
    if evidence_confidence >= 0.62 and not _too_many_missing(missing_fields):
        return "medium"
    return "low"


def _headline(decision: AlertDecision, event: Any, materiality: int, impact: ImpactDirection) -> str:
    action = "Alert" if decision == AlertDecision.ALERT else "Review" if decision == AlertDecision.REVIEW else "Stay quiet"
    return f"{action}: {event.type.value} is {materiality}/100 material here ({impact.value} impact, {event.stage.value})."


def _quiet_reason(materiality: int, physical_score: float, scope_fit: float, missing_fields: list[str]) -> str:
    if physical_score < 0.30:
        return "The event may matter elsewhere, but this coordinate does not appear physically positioned to benefit or lose option value."
    if scope_fit < 0.30:
        return "The event scope appears too far or too weakly related to this coordinate."
    if missing_fields:
        return "The score is low with the available fields; fetch missing fields before treating silence as final."
    return f"Materiality score {materiality}/100 is below the alert threshold."


def _next_best_action(decision: AlertDecision, missing_fields: list[str], event_type: EventType) -> str | None:
    if missing_fields:
        return "Fetch missing Mireye fields only if this event passed watcher relevance and scope gates: " + ", ".join(missing_fields)
    if decision == AlertDecision.REVIEW:
        return "Have a human check event scope geometry and stage language before notifying."
    if event_type in {EventType.DATA_CENTER_MORATORIUM, EventType.BESS_MORATORIUM}:
        return "If monitoring nearby jurisdictions, score adjacent high-optionality sites for spillover value."
    return None


def _threshold_score(value_: float | None, thresholds: list[tuple[float, float]], default: float) -> float:
    if value_ is None:
        return default
    for threshold, score in thresholds:
        if value_ >= threshold:
            return score
    return 0.12


def _inverse_distance_score(value_: float | None, thresholds: list[tuple[float, float]]) -> float:
    if value_ is None:
        return 0.50
    for threshold, score in thresholds:
        if value_ <= threshold:
            return score
    return 0.05


def _citations(fields: dict[str, Any], names: list[str]) -> list[dict[str, Any]]:
    citations = []
    for name in names:
        cited = citation_for(field(fields, name))
        if cited:
            citations.append(cited)
    return citations


def _too_many_missing(missing_fields: list[str]) -> bool:
    return len(missing_fields) >= 3


def _fmt(value_: float | None, unit: str) -> str:
    if value_ is None:
        return "unknown"
    return f"{value_:g} {unit}"


def _clamp(value_: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value_))
