from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import mean
from typing import Any

from .models import AlertDecision, DecisionRequest
from .scoring import score_decision


@dataclass(frozen=True)
class ReplaySummary:
    total: int
    alerts: int
    reviews: int
    quiet: int
    true_positives: int
    false_positives: int
    misses: int
    precision: float | None
    recall: float | None
    avg_lead_days_vs_adoption: float | None
    avg_lead_days_vs_press: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "alerts": self.alerts,
            "reviews": self.reviews,
            "quiet": self.quiet,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "misses": self.misses,
            "precision": self.precision,
            "recall": self.recall,
            "avg_lead_days_vs_adoption": self.avg_lead_days_vs_adoption,
            "avg_lead_days_vs_press": self.avg_lead_days_vs_press,
        }


def replay(records: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = []
    lead_vs_adoption = []
    lead_vs_press = []
    true_positives = 0
    false_positives = 0
    misses = 0

    for record in records:
        response = score_decision(DecisionRequest.from_dict(record))
        labels = record.get("labels", {}) or {}
        is_material = bool(labels.get("material"))
        is_alert = response.decision == AlertDecision.ALERT

        if is_alert and is_material:
            true_positives += 1
        elif is_alert and not is_material:
            false_positives += 1
        elif not is_alert and is_material:
            misses += 1

        event_date = _parse_date(record.get("event", {}).get("published_at"))
        adoption_date = _parse_date(labels.get("adoption_date"))
        press_date = _parse_date(labels.get("first_press_date"))
        if is_alert and event_date and adoption_date:
            lead_vs_adoption.append((adoption_date - event_date).days)
        if is_alert and event_date and press_date:
            lead_vs_press.append((press_date - event_date).days)

        decisions.append(
            {
                "event_id": record.get("event", {}).get("id"),
                "decision": response.decision.value,
                "materiality_score": response.materiality_score,
                "label_material": is_material,
            }
        )

    alerts = sum(1 for item in decisions if item["decision"] == AlertDecision.ALERT.value)
    reviews = sum(1 for item in decisions if item["decision"] == AlertDecision.REVIEW.value)
    quiet = sum(1 for item in decisions if item["decision"] == AlertDecision.QUIET.value)
    precision = true_positives / alerts if alerts else None
    material_count = true_positives + misses
    recall = true_positives / material_count if material_count else None

    summary = ReplaySummary(
        total=len(records),
        alerts=alerts,
        reviews=reviews,
        quiet=quiet,
        true_positives=true_positives,
        false_positives=false_positives,
        misses=misses,
        precision=precision,
        recall=recall,
        avg_lead_days_vs_adoption=mean(lead_vs_adoption) if lead_vs_adoption else None,
        avg_lead_days_vs_press=mean(lead_vs_press) if lead_vs_press else None,
    )
    return {"summary": summary.as_dict(), "decisions": decisions}


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
