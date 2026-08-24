from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    DATA_CENTER_MORATORIUM = "data_center_moratorium"
    BESS_MORATORIUM = "bess_moratorium"
    REZONING = "rezoning"
    ANNEXATION = "annexation"
    COMP_PLAN_AMENDMENT = "comp_plan_amendment"
    UTILITY_EXTENSION = "utility_extension"
    BIG_PERMIT = "big_permit"
    UNKNOWN = "unknown"


class EventStage(StrEnum):
    PROPOSED = "proposed"
    HEARD = "heard"
    ADOPTED = "adopted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    TABLED = "tabled"
    UNKNOWN = "unknown"


class AlertDecision(StrEnum):
    ALERT = "alert"
    REVIEW = "review"
    QUIET = "quiet"


class ImpactDirection(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EventScope:
    relation_to_site: str = "unknown"
    distance_m: float | None = None
    description: str | None = None
    geometry_ref: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EventScope":
        data = data or {}
        return cls(
            relation_to_site=str(data.get("relation_to_site", "unknown")).lower(),
            distance_m=_optional_float(data.get("distance_m")),
            description=data.get("description"),
            geometry_ref=data.get("geometry_ref"),
        )


@dataclass(frozen=True)
class Event:
    id: str
    type: EventType
    stage: EventStage
    title: str
    jurisdiction: str
    subject: str | None = None
    confidence: float | None = None
    published_at: str | None = None
    source_url: str | None = None
    source_quote: str | None = None
    detected_terms: tuple[str, ...] = ()
    scope: EventScope = field(default_factory=EventScope)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            id=str(data.get("id") or data.get("event_id") or data.get("canonical_id") or ""),
            type=_event_type_from(data.get("type") or data.get("event_type"), data.get("subject")),
            stage=_event_stage_from(data.get("stage")),
            title=str(data.get("title", "")),
            jurisdiction=str(data.get("jurisdiction", "")),
            subject=data.get("subject"),
            confidence=_optional_float(data.get("confidence")),
            published_at=data.get("published_at") or data.get("first_seen_at") or data.get("introduced_at"),
            source_url=data.get("source_url") or _first_evidence_value(data.get("evidence"), "source_url"),
            source_quote=data.get("source_quote") or _first_evidence_value(data.get("evidence"), "passage"),
            detected_terms=tuple(data.get("detected_terms", ()) or ()),
            scope=EventScope.from_dict(data.get("scope")),
        )


@dataclass(frozen=True)
class Site:
    id: str
    lat: float
    lng: float
    label: str | None = None
    address: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Site":
        return cls(
            id=str(data.get("id", "")),
            lat=float(data["lat"]),
            lng=float(data["lng"]),
            label=data.get("label"),
            address=data.get("address"),
        )


@dataclass(frozen=True)
class MireyeField:
    name: str
    value: Any
    unit: str | None = None
    source: str | None = None
    source_url: str | None = None
    confidence: str | float | None = None
    fetched_at: str | None = None
    status: str = "ok"
    license: str | None = None
    stale: bool = False
    profile: str | None = None
    notes: str | None = None

    @classmethod
    def from_raw(cls, name: str, raw: Any) -> "MireyeField":
        if isinstance(raw, dict) and ("value" in raw or "status" in raw):
            return cls(
                name=name,
                value=raw.get("value"),
                unit=raw.get("unit"),
                source=raw.get("source"),
                source_url=raw.get("source_url"),
                confidence=raw.get("confidence"),
                fetched_at=raw.get("fetched_at"),
                status=str(raw.get("status", "ok")).lower(),
                license=raw.get("license"),
                stale=bool(raw.get("stale", False)),
                profile=raw.get("profile"),
                notes=raw.get("notes"),
            )
        return cls(name=name, value=raw)


@dataclass(frozen=True)
class DecisionRequest:
    event: Event
    site: Site
    fields: dict[str, MireyeField]
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionRequest":
        mireye = data.get("mireye") or data.get("physical") or {}
        raw_fields = _extract_mireye_fields(mireye)
        fields = {
            name: MireyeField.from_raw(name, value)
            for name, value in raw_fields.items()
            if name not in {"lat", "lng", "fetched_at", "partial_failures", "geocode", "cache"}
        }
        return cls(
            event=Event.from_dict(data["event"]),
            site=Site.from_dict(data["site"]),
            fields=fields,
            raw=data,
        )


@dataclass(frozen=True)
class ScoreBreakdown:
    event_relevance: float
    stage_weight: float
    scope_fit: float
    physical_optionality: float
    evidence_confidence: float

    def as_dict(self) -> dict[str, float]:
        return {
            "event_relevance": self.event_relevance,
            "stage_weight": self.stage_weight,
            "scope_fit": self.scope_fit,
            "physical_optionality": self.physical_optionality,
            "evidence_confidence": self.evidence_confidence,
        }


@dataclass(frozen=True)
class DecisionResponse:
    decision: AlertDecision
    materiality_score: int
    confidence: str
    impact_direction: ImpactDirection
    event_stage: EventStage
    headline: str
    rationale: list[str]
    quiet_reason: str | None
    missing_fields: list[str]
    required_fields: list[str]
    score_breakdown: ScoreBreakdown
    citations: list[dict[str, Any]]
    next_best_action: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "materiality_score": self.materiality_score,
            "confidence": self.confidence,
            "impact_direction": self.impact_direction.value,
            "event_stage": self.event_stage.value,
            "headline": self.headline,
            "rationale": self.rationale,
            "quiet_reason": self.quiet_reason,
            "missing_fields": self.missing_fields,
            "required_fields": self.required_fields,
            "score_breakdown": self.score_breakdown.as_dict(),
            "citations": self.citations,
            "next_best_action": self.next_best_action,
        }


def _enum_or(enum_type: type[StrEnum], value: Any, default: StrEnum) -> Any:
    try:
        return enum_type(str(value).lower())
    except (TypeError, ValueError):
        return default


def _event_stage_from(value: Any) -> EventStage:
    return _enum_or(EventStage, value, EventStage.UNKNOWN)


def _event_type_from(value: Any, subject: Any = None) -> EventType:
    normalized = str(value or "").strip().lower()
    normalized = normalized.replace("-", "_").replace(" ", "_")
    subject_text = str(subject or "").strip().lower()
    if normalized == "moratorium":
        if any(term in subject_text for term in ("bess", "battery", "batteries", "energy storage")):
            return EventType.BESS_MORATORIUM
        if any(term in subject_text for term in ("data center", "data centre", "datacenter")):
            return EventType.DATA_CENTER_MORATORIUM
        return EventType.DATA_CENTER_MORATORIUM
    mapping = {
        "major_development_permit": EventType.BIG_PERMIT,
        "permit": EventType.BIG_PERMIT,
        "big_permit": EventType.BIG_PERMIT,
        "comp_plan_amendment": EventType.COMP_PLAN_AMENDMENT,
        "comprehensive_plan_amendment": EventType.COMP_PLAN_AMENDMENT,
    }
    if normalized in mapping:
        return mapping[normalized]
    return _enum_or(EventType, normalized, EventType.UNKNOWN)


def _extract_mireye_fields(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("fields"), dict):
        return payload["fields"]
    if isinstance(payload.get("datapoints"), list):
        return _datapoints_to_fields(payload["datapoints"])

    fields: dict[str, Any] = {}
    for key, value in payload.items():
        if key in {"cache", "site", "quote", "budget", "bundles"}:
            if isinstance(value, dict):
                fields.update(_extract_mireye_fields(value))
            elif isinstance(value, list):
                for item in value:
                    fields.update(_extract_mireye_fields(item))
            continue
        if isinstance(value, dict) and isinstance(value.get("datapoints"), list):
            fields.update(_datapoints_to_fields(value["datapoints"]))
        elif isinstance(value, dict) and ("value" in value or "status" in value):
            fields[key] = value
    return fields


def _datapoints_to_fields(datapoints: list[Any]) -> dict[str, Any]:
    fields = {}
    for item in datapoints:
        if not isinstance(item, dict):
            continue
        name = item.get("field_name") or item.get("name") or item.get("field")
        if name:
            fields[str(name)] = item
    return fields


def _first_evidence_value(evidence: Any, key: str) -> Any:
    if not isinstance(evidence, list):
        return None
    for item in evidence:
        if isinstance(item, dict) and item.get(key):
            return item[key]
    return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
