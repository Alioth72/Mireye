"""
Canonical event identity and stage-transition upsert logic.

canonical_id is the dedup key: it must be stable across the many documents
(agenda, staff report, minutes, amendment, final vote) that can all refer to
the same underlying legislation.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import Event, EventStage, EventVersion, STAGE_ORDER

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.strip().lower()).strip("-")


def canonical_event_id(
    jurisdiction: str,
    source: str,
    external_legislation_id: str | None,
    *,
    fallback_title: str | None = None,
    fallback_date_bucket: str | None = None,
) -> str:
    """
    Preferred path: a real bill/legislation number from the source
    (e.g. Legistar MatterFile "CB 121214") gives a clean, stable id:

        seattle:seattle_legistar:cb-121214

    Fallback path (RISK -- see design notes): when no legislation id is
    available (e.g. a bare agenda item with no bill number), we hash a
    normalized title + a coarse date bucket. This is intentionally weaker
    and more collision-prone; treat any event created via this path as lower
    confidence and consider surfacing it for manual review rather than
    auto-alerting.
    """
    jur = _slugify(jurisdiction)
    src = _slugify(source)

    if external_legislation_id:
        return f"{jur}:{src}:{_slugify(external_legislation_id)}"

    if not fallback_title:
        raise ValueError(
            "canonical_event_id requires either external_legislation_id or fallback_title"
        )
    basis = f"{_slugify(fallback_title)}|{fallback_date_bucket or ''}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"{jur}:{src}:fallback:{digest}"


def upsert_event(
    session: Session,
    *,
    canonical_id: str,
    event_type,
    title: str,
    description: str | None,
    jurisdiction: str,
    stage: EventStage,
    stage_occurred_at: datetime | None,
    confidence: float,
    document_id: str | None,
    subject: str | None = None,
    geography_type=None,
    geography: dict | None = None,
) -> tuple[Event, bool, bool]:
    """
    Create or update the Event for `canonical_id`.

    Returns (event, was_created, stage_changed). `stage_changed` is what the
    downstream monitor cares about -- it's the trigger for a new alert
    evaluation, not "a new document arrived".

    Stage only ever moves forward according to STAGE_ORDER (or to a terminal
    off-path stage like REJECTED/WITHDRAWN/TABLED). A document that would
    regress an already-more-advanced event (e.g. a stale/out-of-order
    "introduced" record arriving after we've already recorded ADOPTED) is
    recorded as a version row for history but does not change Event.stage.
    """
    existing = session.query(Event).filter_by(canonical_id=canonical_id).one_or_none()

    now = datetime.now(timezone.utc)

    if existing is None:
        event = Event(
            canonical_id=canonical_id,
            event_type=event_type,
            title=title,
            description=description,
            subject=subject,
            jurisdiction=jurisdiction,
            stage=stage,
            confidence=confidence,
            first_seen_at=now,
            last_seen_at=now,
        )
        _apply_stage_timestamp(event, stage, stage_occurred_at)
        if geography_type is not None:
            event.geography_type = geography_type
        if geography is not None:
            event.geography = geography
        session.add(event)
        session.flush()  # get event.id

        session.add(
            EventVersion(
                event_id=event.id,
                document_id=document_id,
                stage=stage,
                occurred_at=stage_occurred_at,
            )
        )
        return event, True, True

    stage_changed = existing.stage != stage
    is_forward = STAGE_ORDER.get(stage, -1) >= STAGE_ORDER.get(existing.stage, -1)

    existing.last_seen_at = now
    # keep the higher-confidence description/title if this update is weaker
    if confidence >= existing.confidence:
        existing.title = title
        existing.description = description or existing.description
        existing.subject = subject or existing.subject
        existing.confidence = confidence

    if geography_type is not None and existing.geography_type.name == "UNRESOLVED":
        existing.geography_type = geography_type
        existing.geography = geography or {}

    if stage_changed and is_forward:
        existing.stage = stage
        _apply_stage_timestamp(existing, stage, stage_occurred_at)

    # Always record a version row for provenance/replay, even if the stage
    # didn't move forward -- but only mark stage_changed=True (the alert
    # trigger) when it actually advanced.
    session.add(
        EventVersion(
            event_id=existing.id,
            document_id=document_id,
            stage=stage,
            occurred_at=stage_occurred_at,
        )
    )

    return existing, False, bool(stage_changed and is_forward)


def _apply_stage_timestamp(event: Event, stage: EventStage, occurred_at: datetime | None) -> None:
    if occurred_at is None:
        return
    if stage == EventStage.PROPOSED and event.introduced_at is None:
        event.introduced_at = occurred_at
    elif stage == EventStage.HEARD:
        event.heard_at = occurred_at
    elif stage == EventStage.ADOPTED:
        event.adopted_at = occurred_at
