"""End-to-end: a real Phase 1 Event (through canonicalize.upsert_event, the same path
ingest.py uses) combined with real Phase 2 physical data (through the fake Mireye
transport) via phase3.pipeline.decide(). These are the false-positive scenarios the
system is required to be conservative about, plus the positive ALERT control and the
dedup/idempotency guarantee.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool as SAStaticPool
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from monitor_records.api import _event_to_json
from monitor_records.canonicalize import canonical_event_id, upsert_event
from monitor_records.models import Base as P1Base, EventStage, EventType, GeographyType

from phase2.config import Settings
from phase2.mireye.client import MireyeClient
from phase2.models import FetchLog, Site

from phase3.models import P3Decision
from phase3.pipeline import decide

from tests.fakes.mireye_fake import BAD_SITE, GOOD_SITE, make_transport


@pytest.fixture()
def p1_session():
    engine = sa_create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=SAStaticPool)
    P1Base.metadata.create_all(engine)
    Session_ = sessionmaker(bind=engine)
    s = Session_()
    yield s
    s.close()


@pytest.fixture()
def p23_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture()
def p2_session(p23_engine):
    with Session(p23_engine) as s:
        yield s


@pytest.fixture()
def p3_session(p23_engine):
    with Session(p23_engine) as s:
        yield s


@pytest.fixture()
async def client():
    settings = Settings(mireye_api_token="fake-token")
    c = MireyeClient(settings=settings, transport=make_transport())
    async with c:
        yield c


@pytest.fixture()
def good_site(p2_session):
    site = Site(lat=GOOD_SITE[0], lng=GOOD_SITE[1], political_locality="Seattle", political_region="Washington")
    p2_session.add(site)
    p2_session.commit()
    p2_session.refresh(site)
    return site


@pytest.fixture()
def bad_site(p2_session):
    site = Site(lat=BAD_SITE[0], lng=BAD_SITE[1], political_locality="Seattle", political_region="Washington")
    p2_session.add(site)
    p2_session.commit()
    p2_session.refresh(site)
    return site


def _make_event(
    p1_session,
    *,
    bill: str,
    stage: EventStage,
    confidence: float,
    subject: str | None = "data centers",
    jurisdiction: str = "Seattle",
    geography_type: GeographyType = GeographyType.JURISDICTION,
    geography: dict | None = None,
) -> dict:
    cid = canonical_event_id("Seattle", "seattle_legistar", bill)
    event_row, _, _ = upsert_event(
        p1_session,
        canonical_id=cid,
        event_type=EventType.MORATORIUM,
        title="An ordinance imposing a moratorium on new data centers",
        description="Citywide moratorium on data center development permits.",
        subject=subject,
        jurisdiction=jurisdiction,
        stage=stage,
        stage_occurred_at=None,
        confidence=confidence,
        document_id=None,
        geography_type=geography_type,
        geography=geography if geography is not None else {"name": jurisdiction},
    )
    return _event_to_json(event_row)


# --------------------------------------------------------------------------
# positive control
# --------------------------------------------------------------------------
async def test_adopted_high_confidence_matching_geography_material_site_alerts(
    p1_session, p2_session, p3_session, client, good_site
):
    event = _make_event(p1_session, bill="CB1", stage=EventStage.ADOPTED, confidence=0.92)
    result = await decide(event, good_site.id, p2_session=p2_session, p3_session=p3_session, client=client)
    assert result.decision == "ALERT"
    assert result.government_evidence == event["evidence"]
    assert result.metric == "data_center_optionality"
    assert result.score >= 0.5


# --------------------------------------------------------------------------
# req: proposed events must not be treated as adopted
# --------------------------------------------------------------------------
async def test_proposed_event_never_alerts_even_on_a_material_site(
    p1_session, p2_session, p3_session, client, good_site
):
    event = _make_event(p1_session, bill="CB2", stage=EventStage.PROPOSED, confidence=0.9)
    result = await decide(event, good_site.id, p2_session=p2_session, p3_session=p3_session, client=client)
    assert result.decision == "SILENCE"
    assert "PROPOSED" in result.reasons[0]
    # never even touched Mireye -- the stage gate short-circuits before any bundle fetch
    assert result.metric is None


# (PROPOSED/HEARD/WITHDRAWN/TABLED stage-gate coverage lives in test_stage_geography_gates.py)


# --------------------------------------------------------------------------
# req: keyword-only / low-confidence matches must not alert
# --------------------------------------------------------------------------
async def test_heuristic_fallback_confidence_never_alerts(p1_session, p2_session, p3_session, client, good_site):
    """0.4 is the exact confidence monitor_records/ingest.py hardcodes for its no-LLM
    heuristic fallback path -- effectively a keyword match with no real understanding of
    the text. Even ADOPTED + a physically ideal site must not alert here."""
    event = _make_event(p1_session, bill="CB3", stage=EventStage.ADOPTED, confidence=0.4, subject=None)
    result = await decide(event, good_site.id, p2_session=p2_session, p3_session=p3_session, client=client)
    assert result.decision == "SILENCE"
    assert "confidence" in result.reasons[0]


# --------------------------------------------------------------------------
# req: events outside the monitored geography must not alert
# --------------------------------------------------------------------------
async def test_event_in_a_different_jurisdiction_does_not_alert(
    p1_session, p2_session, p3_session, client, good_site
):
    event = _make_event(p1_session, bill="CB4", stage=EventStage.ADOPTED, confidence=0.9, jurisdiction="Bellevue")
    result = await decide(event, good_site.id, p2_session=p2_session, p3_session=p3_session, client=client)
    assert result.decision == "SILENCE"
    assert "Bellevue" in result.reasons[0]
    assert result.metric is None  # never reached the physical evaluation


async def test_unresolved_geography_never_alerts(p1_session, p2_session, p3_session, client, good_site):
    event = _make_event(
        p1_session, bill="CB5", stage=EventStage.ADOPTED, confidence=0.9, geography_type=GeographyType.UNRESOLVED,
        geography={},
    )
    result = await decide(event, good_site.id, p2_session=p2_session, p3_session=p3_session, client=client)
    assert result.decision == "SILENCE"
    assert result.metric is None


# --------------------------------------------------------------------------
# req: physically irrelevant events must not alert
# --------------------------------------------------------------------------
async def test_adopted_event_on_a_physically_foreclosed_site_does_not_alert(
    p1_session, p2_session, p3_session, client, bad_site
):
    """Same ADOPTED, high-confidence, correctly-scoped event as the positive control --
    only the site's physical profile differs. A data-center moratorium removes no real
    option value at a site that was never buildable for one."""
    event = _make_event(p1_session, bill="CB6", stage=EventStage.ADOPTED, confidence=0.92)
    result = await decide(event, bad_site.id, p2_session=p2_session, p3_session=p3_session, client=client)
    assert result.decision == "SILENCE"
    assert result.metric == "data_center_optionality"
    assert result.score < 0.5
    assert "does not materially change" in result.reasons[0]


# --------------------------------------------------------------------------
# req: one government action -> one alert, even under repeated/retried calls
# --------------------------------------------------------------------------
async def test_repeat_call_replays_the_cached_decision_without_recomputing(
    p1_session, p2_session, p3_session, client, good_site
):
    event = _make_event(p1_session, bill="CB7", stage=EventStage.ADOPTED, confidence=0.92)
    first = await decide(event, good_site.id, p2_session=p2_session, p3_session=p3_session, client=client)
    assert first.replayed is False

    fetch_count_before = len(p2_session.exec(select_all_fetch_logs()).all())

    second = await decide(event, good_site.id, p2_session=p2_session, p3_session=p3_session, client=client)
    assert second.replayed is True
    assert second.decision == first.decision

    fetch_count_after = len(p2_session.exec(select_all_fetch_logs()).all())
    assert fetch_count_after == fetch_count_before  # no new Mireye spend on replay

    decisions = p3_session.exec(select_all_decisions()).all()
    assert len(decisions) == 1  # exactly one persisted decision for this canonical_id/stage/site


async def test_repeat_call_on_a_stage_gated_silence_does_not_crash_or_duplicate(
    p1_session, p2_session, p3_session, client, good_site
):
    """Regression test: the dedup check must run before EVERY gate that can persist a
    decision, not just before the physical-evaluation step. A stage-gated SILENCE (e.g.
    a low-confidence heuristic match on an ADOPTED bill) persists too -- an orchestrator
    that calls decide() again for the same key (a retry, or simply being called twice)
    must replay that cached SILENCE, not attempt a second INSERT and violate the
    (canonical_id, stage, confidence_bucket, site_id) unique constraint."""
    event = _make_event(p1_session, bill="CB9", stage=EventStage.ADOPTED, confidence=0.4, subject=None)

    first = await decide(event, good_site.id, p2_session=p2_session, p3_session=p3_session, client=client)
    assert first.decision == "SILENCE"
    assert first.replayed is False

    second = await decide(event, good_site.id, p2_session=p2_session, p3_session=p3_session, client=client)
    assert second.decision == "SILENCE"
    assert second.replayed is True

    decisions = p3_session.exec(select_all_decisions()).all()
    assert len(decisions) == 1


async def test_confidence_upgrade_on_same_stage_is_not_masked_by_a_stale_cached_silence(
    p1_session, p2_session, p3_session, client, good_site
):
    """Walkthrough: doc 1 hits the heuristic fallback (confidence 0.4) for an
    already-ADOPTED bill -- correctly SILENCEd and cached. Doc 2 (e.g. attachment
    extraction comes online, or the LLM key is added) re-extracts the SAME bill at the
    SAME stage with confidence 0.9. Because Event.stage did not change, an orchestrator
    that only re-invokes Phase 3 on stage_changed would never call decide() again -- but
    if it IS called again, the dedup key must not replay the old, now-wrong SILENCE."""
    cid = canonical_event_id("Seattle", "seattle_legistar", "CB8")
    low_conf_row, _, _ = upsert_event(
        p1_session, canonical_id=cid, event_type=EventType.MORATORIUM, title="X", description=None,
        subject=None, jurisdiction="Seattle", stage=EventStage.ADOPTED, stage_occurred_at=None,
        confidence=0.4, document_id="d1", geography_type=GeographyType.JURISDICTION, geography={"name": "Seattle"},
    )
    low_conf_event = _event_to_json(low_conf_row)
    first = await decide(low_conf_event, good_site.id, p2_session=p2_session, p3_session=p3_session, client=client)
    assert first.decision == "SILENCE"

    high_conf_row, _, _ = upsert_event(
        p1_session, canonical_id=cid, event_type=EventType.MORATORIUM, title="X", description="real text",
        subject="data centers", jurisdiction="Seattle", stage=EventStage.ADOPTED, stage_occurred_at=None,
        confidence=0.9, document_id="d2", geography_type=GeographyType.JURISDICTION, geography={"name": "Seattle"},
    )
    high_conf_event = _event_to_json(high_conf_row)
    second = await decide(high_conf_event, good_site.id, p2_session=p2_session, p3_session=p3_session, client=client)

    assert second.replayed is False  # a genuinely new decision, not a replay of the stale SILENCE
    assert second.decision == "ALERT"


def select_all_fetch_logs():
    from sqlmodel import select

    return select(FetchLog)


def select_all_decisions():
    from sqlmodel import select

    return select(P3Decision)
