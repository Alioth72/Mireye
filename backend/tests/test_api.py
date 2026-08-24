"""JSON contract shape -- the interface Phase 2/3 actually consume."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from monitor_records.api import _event_to_json
from monitor_records.canonicalize import canonical_event_id, upsert_event
from monitor_records.models import Base, Event, EventStage, EventType


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def test_event_json_includes_subject(session):
    """D9: Phase 3's bundle selection reads `subject` off the wire JSON -- it must be
    present in the exported shape, not just on the ORM model."""
    cid = canonical_event_id("Seattle", "seattle_legistar", "CB121214")
    upsert_event(
        session,
        canonical_id=cid,
        event_type=EventType.MORATORIUM,
        title="An ordinance on data centers",
        description="...",
        subject="data centers",
        jurisdiction="Seattle",
        stage=EventStage.ADOPTED,
        stage_occurred_at=datetime(2026, 6, 9),
        confidence=0.9,
        document_id="d1",
    )
    event = session.query(Event).filter_by(canonical_id=cid).one()
    payload = _event_to_json(event)

    assert payload["subject"] == "data centers"
    assert payload["event_type"] == "MORATORIUM"
    assert payload["stage"] == "ADOPTED"
