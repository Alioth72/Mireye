from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from monitor_records.canonicalize import canonical_event_id, upsert_event
from monitor_records.models import Base, EventStage, EventType


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def test_canonical_id_stable_for_same_bill_number():
    a = canonical_event_id("Seattle", "seattle_legistar", "CB 121214")
    b = canonical_event_id("Seattle", "seattle_legistar", "cb-121214")
    assert a == b


def test_canonical_id_fallback_requires_title():
    with pytest.raises(ValueError):
        canonical_event_id("Seattle", "seattle_legistar", None)


def test_canonical_id_fallback_is_hash_based():
    cid = canonical_event_id(
        "Seattle", "seattle_legistar", None, fallback_title="Data center moratorium"
    )
    assert cid.startswith("seattle:seattle-legistar:fallback:")


def test_multiple_documents_same_bill_map_to_one_event(session):
    """This is the core requirement: CB121214's agenda, staff report, minutes,
    amendment, and final vote must all collapse into ONE Event."""
    cid = canonical_event_id("Seattle", "seattle_legistar", "CB121214")

    doc_titles = ["Agenda", "Staff report", "Hearing minutes", "Amendment", "Final vote"]
    stages = [
        EventStage.PROPOSED,
        EventStage.PROPOSED,
        EventStage.HEARD,
        EventStage.HEARD,
        EventStage.ADOPTED,
    ]

    for i, (doc_title, stage) in enumerate(zip(doc_titles, stages)):
        event, created, changed = upsert_event(
            session,
            canonical_id=cid,
            event_type=EventType.MORATORIUM,
            title="Data-center moratorium",
            description=doc_title,
            jurisdiction="Seattle",
            stage=stage,
            stage_occurred_at=datetime(2026, 6, 1 + i),
            confidence=0.9,
            document_id=f"doc-{i}",
        )

    from monitor_records.models import Event

    assert session.query(Event).count() == 1
    final = session.query(Event).filter_by(canonical_id=cid).one()
    assert final.stage == EventStage.ADOPTED
    assert len(final.versions) == 5


def test_stage_never_regresses(session):
    cid = canonical_event_id("Seattle", "seattle_legistar", "CB999999")

    upsert_event(
        session,
        canonical_id=cid,
        event_type=EventType.REZONING,
        title="Some rezoning",
        description=None,
        jurisdiction="Seattle",
        stage=EventStage.ADOPTED,
        stage_occurred_at=datetime(2026, 6, 9),
        confidence=0.9,
        document_id="doc-final",
    )

    # a stale/out-of-order "introduced" document arrives after adoption
    event, created, changed = upsert_event(
        session,
        canonical_id=cid,
        event_type=EventType.REZONING,
        title="Some rezoning",
        description=None,
        jurisdiction="Seattle",
        stage=EventStage.PROPOSED,
        stage_occurred_at=datetime(2026, 5, 1),
        confidence=0.9,
        document_id="doc-stale",
    )

    assert event.stage == EventStage.ADOPTED  # did not regress
    assert created is False
    assert changed is False  # should not re-trigger an alert
    # but the stale document is still recorded for provenance/replay
    assert len(event.versions) == 2


def test_stage_change_flag_only_true_on_forward_progress(session):
    cid = canonical_event_id("Seattle", "seattle_legistar", "CB555555")

    _, created1, changed1 = upsert_event(
        session,
        canonical_id=cid,
        event_type=EventType.MORATORIUM,
        title="X",
        description=None,
        jurisdiction="Seattle",
        stage=EventStage.PROPOSED,
        stage_occurred_at=datetime(2026, 6, 1),
        confidence=0.8,
        document_id="d1",
    )
    assert created1 is True
    assert changed1 is True  # creation always counts as a change

    _, created2, changed2 = upsert_event(
        session,
        canonical_id=cid,
        event_type=EventType.MORATORIUM,
        title="X",
        description=None,
        jurisdiction="Seattle",
        stage=EventStage.PROPOSED,  # same stage again (e.g. duplicate doc)
        stage_occurred_at=datetime(2026, 6, 1),
        confidence=0.8,
        document_id="d2",
    )
    assert created2 is False
    assert changed2 is False


def test_subject_propagates_through_create_and_update(session):
    """D9: subject (e.g. "data centers" vs "BESS") must survive upsert_event, since
    Phase 3's bundle selection branches on it -- a data-center moratorium and a BESS
    moratorium are both event_type MORATORIUM but need different physical bundles."""
    cid = canonical_event_id("Seattle", "seattle_legistar", "CB121214")

    event, created, _ = upsert_event(
        session,
        canonical_id=cid,
        event_type=EventType.MORATORIUM,
        title="An ordinance on data centers",
        description=None,
        subject="data centers",
        jurisdiction="Seattle",
        stage=EventStage.PROPOSED,
        stage_occurred_at=datetime(2026, 6, 1),
        confidence=0.9,
        document_id="d1",
    )
    assert created is True
    assert event.subject == "data centers"

    # a later update that clears the title/description path (confidence high enough to
    # win the update) but supplies no subject must not clobber the previously-known one
    event, _, _ = upsert_event(
        session,
        canonical_id=cid,
        event_type=EventType.MORATORIUM,
        title="An ordinance on data centers",
        description=None,
        subject=None,
        jurisdiction="Seattle",
        stage=EventStage.HEARD,
        stage_occurred_at=datetime(2026, 6, 3),
        confidence=0.9,  # >= existing.confidence, so the title/description/subject branch runs
        document_id="d2",
    )
    assert event.subject == "data centers"
