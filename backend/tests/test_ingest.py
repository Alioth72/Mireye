"""
End-to-end ingestion pipeline test.

Uses a FakeSource (implements RecordSource) instead of the real Legistar
adapter, so this runs with no network and no LLM key. It proves the full
chain -- discover -> fetch -> persist Documents -> classify -> deterministic
stage resolution -> canonicalize/upsert -> Evidence -- actually wires
together end to end, using the heuristic (no-LLM) fallback path.

This is the same shape as the real historical-replay scenario from the
design doc: one bill (CB121214, a data-center moratorium) moving through
PROPOSED -> HEARD -> ADOPTED across three separate fetches, each with an
accumulating MatterHistory.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from monitor_records.ingest import ingest_matter
from monitor_records.models import Base, Event, EventStage
from monitor_records.sources.base import RawRecord, RecordSource


class FakeSource(RecordSource):
    name = "fake_source"

    def __init__(self):
        # external_id -> list[RawRecord], mutated between "ingest runs" to
        # simulate new history rows appearing over time
        self.matters: dict[str, list[RawRecord]] = {}

    async def discover(self, since=None):
        return list(self.matters.keys())

    async def fetch(self, external_id):
        return self.matters[external_id]


def _matter_record(stage_metadata: dict, published_at: datetime) -> RawRecord:
    return RawRecord(
        source="fake_source",
        external_id="CB121214",
        document_type="matter",
        title="An ordinance imposing a temporary moratorium on new data centers",
        source_url="https://seattle.legistar.com/LegislationDetail.aspx?ID=CB121214",
        published_at=published_at,
        meeting_date=published_at,
        raw_text="An ordinance imposing a temporary moratorium on new data center development citywide.",
        metadata=stage_metadata,
    )


def _history_record(action_name: str, passed_flag: bool, when: datetime, idx: int) -> RawRecord:
    return RawRecord(
        source="fake_source",
        external_id=f"CB121214-hist-{idx}",
        document_type="history_action",
        title=action_name,
        source_url=None,
        published_at=when,
        meeting_date=when,
        raw_text=action_name,
        metadata={"action_name": action_name, "passed_flag": passed_flag},
    )


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.mark.asyncio
async def test_full_lifecycle_proposed_heard_adopted(session):
    source = FakeSource()

    # June 1: bill introduced
    source.matters["CB121214"] = [
        _matter_record({"matter_status": "In Committee"}, datetime(2026, 6, 1)),
        _history_record("Introduced", False, datetime(2026, 6, 1), 1),
    ]
    result1 = await ingest_matter(source, session, "CB121214", use_llm=False)
    assert result1.skipped_reason is None
    assert result1.created is True
    assert result1.stage_changed is True
    assert result1.stage == EventStage.PROPOSED.value

    # June 3: public hearing held
    source.matters["CB121214"] = [
        _matter_record({"matter_status": "Heard in Committee"}, datetime(2026, 6, 1)),
        _history_record("Introduced", False, datetime(2026, 6, 1), 1),
        _history_record("Public Hearing held", False, datetime(2026, 6, 3), 2),
    ]
    result2 = await ingest_matter(source, session, "CB121214", use_llm=False)
    assert result2.created is False
    assert result2.stage_changed is True
    assert result2.stage == EventStage.HEARD.value

    # June 9: passed
    source.matters["CB121214"] = [
        _matter_record({"matter_status": "Passed"}, datetime(2026, 6, 1)),
        _history_record("Introduced", False, datetime(2026, 6, 1), 1),
        _history_record("Public Hearing held", False, datetime(2026, 6, 3), 2),
        _history_record("Passed", True, datetime(2026, 6, 9), 3),
    ]
    result3 = await ingest_matter(source, session, "CB121214", use_llm=False)
    assert result3.created is False
    assert result3.stage_changed is True
    assert result3.stage == EventStage.ADOPTED.value

    # exactly ONE event across all three ingests, per the core spec requirement
    assert session.query(Event).count() == 1
    event = session.query(Event).one()
    assert event.canonical_id == result1.canonical_id == result3.canonical_id
    assert event.event_type.value == "MORATORIUM"
    assert len(event.versions) == 3


@pytest.mark.asyncio
async def test_irrelevant_matter_is_skipped(session):
    source = FakeSource()
    source.matters["CB999"] = [
        RawRecord(
            source="fake_source",
            external_id="CB999",
            document_type="matter",
            title="An ordinance renaming a city park bench",
            source_url=None,
            published_at=datetime(2026, 6, 1),
            meeting_date=None,
            raw_text="This ordinance renames a park bench in honor of a retiring librarian.",
            metadata={"matter_status": "In Committee"},
        )
    ]
    result = await ingest_matter(source, session, "CB999", use_llm=False)
    assert result.skipped_reason == "not relevant (keyword filter)"
    assert session.query(Event).count() == 0


@pytest.mark.asyncio
async def test_repeat_ingest_of_same_stage_does_not_duplicate_or_rechange(session):
    source = FakeSource()
    source.matters["CB555"] = [
        _matter_record({"matter_status": "In Committee"}, datetime(2026, 6, 1)),
        _history_record("Introduced", False, datetime(2026, 6, 1), 1),
    ]
    source.matters["CB555"][0].external_id = "CB555"

    r1 = await ingest_matter(source, session, "CB555", use_llm=False)
    r2 = await ingest_matter(source, session, "CB555", use_llm=False)

    assert r1.created is True
    assert r2.created is False
    assert r2.stage_changed is False  # re-ingesting the same state shouldn't re-trigger an alert
    assert session.query(Event).count() == 1
