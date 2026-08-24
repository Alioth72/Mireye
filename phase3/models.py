"""Phase 3's own tables. Table names prefixed `p3_` for the same reason Phase 2 prefixes
`p2_` -- collision-free in a database ever shared with the other phases, though today
each phase gets its own SQLite file (see db.py)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class P3Decision(SQLModel, table=True):
    """One row per (canonical_id, stage, confidence_bucket, site_id). This is what makes
    `pipeline.decide()` idempotent: a repeat call for the same decided-and-confidence-
    bucketed event, at the same site, replays the stored decision instead of re-running
    physical evaluation -- "one government action -> one alert" must hold even under
    retries or at-least-once delivery from whatever calls POST /v1/decide."""

    __tablename__ = "p3_decision"
    __table_args__ = (
        UniqueConstraint(
            "canonical_id", "stage", "confidence_bucket", "site_id", name="uq_p3_decision_key"
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    canonical_id: str = Field(index=True)
    stage: str
    confidence_bucket: str
    site_id: str = Field(index=True)

    decision: str  # "ALERT" | "SILENCE"
    reasons: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    metric: Optional[str] = None
    score: Optional[float] = None
    # {component_name: {score, weight, basis}} -- basis cites the actual field values, so
    # this dict IS the physical evidence trail, not just a score.
    components: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    government_evidence: list[dict] = Field(default_factory=list, sa_column=Column(JSON))

    decided_at: datetime = Field(default_factory=utcnow)
