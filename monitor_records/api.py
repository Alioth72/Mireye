"""
Exposes the clean Event contract to the rest of the team.

Mireye teammate and the monitor/orchestration teammate should only ever need
this endpoint's shape -- they should not need to know Legistar, MatterId,
or anything about the scraper.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.orm import Session

from .db import get_session, init_db
from .models import Event

app = FastAPI(title="Monitor - Public Record Event Intelligence")


def _event_to_json(event: Event) -> dict:
    return {
        "event_id": event.id,
        "canonical_id": event.canonical_id,
        "event_type": event.event_type.value,
        "stage": event.stage.value,
        "title": event.title,
        "description": event.description,
        "jurisdiction": event.jurisdiction,
        "geography": {
            "type": event.geography_type.value,
            **(event.geography or {}),
        },
        "introduced_at": event.introduced_at.isoformat() if event.introduced_at else None,
        "heard_at": event.heard_at.isoformat() if event.heard_at else None,
        "adopted_at": event.adopted_at.isoformat() if event.adopted_at else None,
        "confidence": event.confidence,
        "first_seen_at": event.first_seen_at.isoformat(),
        "last_seen_at": event.last_seen_at.isoformat(),
        "evidence": [
            {
                "source_url": ev.url,
                "document_id": ev.document_id,
                "passage": ev.passage,
                "reason": ev.reason,
            }
            for ev in event.evidence
        ],
    }


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/events")
def list_events(stage: str | None = None, event_type: str | None = None) -> list[dict]:
    with get_session() as session:  # type: Session
        query = session.query(Event)
        if stage:
            query = query.filter(Event.stage == stage)
        if event_type:
            query = query.filter(Event.event_type == event_type)
        return [_event_to_json(e) for e in query.all()]


@app.get("/events/{event_id}")
def get_event(event_id: str) -> dict | None:
    with get_session() as session:  # type: Session
        event = session.get(Event, event_id)
        if event is None:
            return None
        return _event_to_json(event)
