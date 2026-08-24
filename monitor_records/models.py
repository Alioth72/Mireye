"""
Data model for the Public Record / Event Intelligence subsystem.

Design notes
------------
- Document: raw normalized government record (one row per fetched artifact).
- Event: canonical, deduplicated legislative/regulatory event. This is the
  ONLY thing the rest of the team (Mireye layer, monitor/orchestration) should
  consume. See api.py for the exported JSON shape.
- EventVersion: append-only history of stage transitions for an Event. This
  is what lets the monitor say "this is the same event, but its state
  changed" instead of re-alerting on every document.
- Evidence: provenance. Every Event must be traceable back to the exact
  document + passage that justified it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class EventType(str, enum.Enum):
    REZONING = "REZONING"
    ANNEXATION = "ANNEXATION"
    COMP_PLAN_AMENDMENT = "COMP_PLAN_AMENDMENT"
    UTILITY_EXTENSION = "UTILITY_EXTENSION"
    MORATORIUM = "MORATORIUM"
    MAJOR_DEVELOPMENT_PERMIT = "MAJOR_DEVELOPMENT_PERMIT"


class EventStage(str, enum.Enum):
    PROPOSED = "PROPOSED"
    HEARD = "HEARD"
    ADOPTED = "ADOPTED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    TABLED = "TABLED"


# Stages that represent forward progress, in order. Used to guard against a
# document accidentally regressing an event's stage (e.g. a stale document
# processed out of order should not un-adopt an ADOPTED event).
STAGE_ORDER: dict[EventStage, int] = {
    EventStage.PROPOSED: 0,
    EventStage.HEARD: 1,
    EventStage.ADOPTED: 2,
    # terminal/off-path stages: not comparable on the main progression
    EventStage.REJECTED: 2,
    EventStage.WITHDRAWN: 2,
    EventStage.TABLED: 1,
}


class GeographyType(str, enum.Enum):
    JURISDICTION = "JURISDICTION"
    POINT = "POINT"
    POLYGON = "POLYGON"
    UNRESOLVED = "UNRESOLVED"


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_document_source_external_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    source: Mapped[str] = mapped_column(String(64))  # e.g. "seattle_legistar"
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str] = mapped_column(String(128))  # e.g. Legistar MatterId
    document_type: Mapped[str] = mapped_column(String(64))  # bill/agenda/minutes/staff_report/...

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    meeting_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Deterministic id derived from (jurisdiction, source, external_legislation_id),
    # with a fallback strategy for records with no clean bill number.
    # See canonicalize.py. Indexed because it's the dedup lookup key.
    canonical_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)

    event_type: Mapped[EventType] = mapped_column(Enum(EventType))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # e.g. "data centers" vs "BESS" -- both are MORATORIUM events but call for different
    # Phase 2 bundles (fiber is decisive for the first, noise for the second). D9.
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    jurisdiction: Mapped[str] = mapped_column(String(128), default="Seattle")
    stage: Mapped[EventStage] = mapped_column(Enum(EventStage))

    geography_type: Mapped[GeographyType] = mapped_column(
        Enum(GeographyType), default=GeographyType.UNRESOLVED
    )
    geography: Mapped[dict] = mapped_column(JSON, default=dict)

    introduced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heard_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    versions: Mapped[list["EventVersion"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", order_by="EventVersion.created_at"
    )
    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class EventVersion(Base):
    """Append-only stage-transition history for an Event.

    One row per (document, resulting stage). This is what powers replay and
    lets us answer "same event, state changed" instead of re-alerting per
    document.
    """

    __tablename__ = "event_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(String(36), ForeignKey("events.id"))
    document_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("documents.id"), nullable=True)

    stage: Mapped[EventStage] = mapped_column(Enum(EventStage))
    # timestamp the stage transition is attributed to (e.g. MatterPassedDate),
    # NOT when we happened to ingest it -- required for faithful replay.
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    event: Mapped["Event"] = relationship(back_populates="versions")


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_id: Mapped[str] = mapped_column(String(36), ForeignKey("events.id"))
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id"))

    passage: Mapped[str | None] = mapped_column(Text, nullable=True)
    section: Mapped[str | None] = mapped_column(String(128), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # why this passage was cited

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    event: Mapped["Event"] = relationship(back_populates="evidence")
