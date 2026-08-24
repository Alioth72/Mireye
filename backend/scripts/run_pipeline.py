"""End-to-end demo: public government record -> Phase 1 event -> Phase 2 Mireye
physical features -> Phase 3 materiality decision -> ALERT or SILENCE.

Deterministic and offline for Mireye by default -- no network access to api.mireye.com is
used here even though a real MIREYE_API_TOKEN is configured and verified working (see
context/phase3.md D7); Phase 2's leg always runs against the fake transport
(tests/fakes/mireye_fake.py) so the physical-discrimination story (same event, opposite
decisions) stays reproducible and free regardless of what the real API would return today.

Phase 1's LLM extraction is real -- this script does NOT fake or simulate that call.
Provider is whatever monitor_records/extract.py's call_llm_extract() selects (OpenAI
primary, Gemini automatic fallback if OpenAI fails and a Gemini key is configured -- see
LLM_PROVIDER in .env). When no key is configured for either, ingest_matter()'s own
existing fallback quietly drops to the keyword heuristic, so the script still runs either
way with no code change. Live Legistar network access is not used here regardless
(DemoSource supplies canned records for one real bill, CB121214) -- swap in
SeattleLegistarSource for a real fetch.

Usage:
    python scripts/run_pipeline.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, so `monitor_records`/`phase2`/`phase3` import regardless of cwd

from dotenv import load_dotenv
load_dotenv()

import asyncio
import json
from datetime import datetime

from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool as SAStaticPool
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from monitor_records.api import _event_to_json
from monitor_records.ingest import ingest_matter
from monitor_records.models import Base as P1Base, Event
from monitor_records.sources.base import RawRecord, RecordSource

from phase2.config import Settings
from phase2.mireye.client import MireyeClient
from phase2.models import Site

from phase3.pipeline import decide

from tests.fakes.mireye_fake import BAD_SITE, GOOD_SITE, make_transport


class DemoSource(RecordSource):
    """The exact scenario from Phase 1's own test suite (tests/test_ingest.py): one bill,
    CB121214, a data-center moratorium moving PROPOSED -> HEARD -> ADOPTED across three
    separate fetches, each with an accumulating MatterHistory. This proves the real
    ingest_matter() pipeline -- not a shortcut around it -- collapses that into one
    canonical Event, exactly as it would against real Legistar data.
    """

    name = "demo_legistar"

    def __init__(self):
        self.matters: dict[str, list[RawRecord]] = {}

    async def discover(self, since=None):
        return list(self.matters.keys())

    async def fetch(self, external_id):
        return self.matters[external_id]


_MATTER_TEXT = "An ordinance imposing a temporary moratorium on new data center development citywide."
# The fuller text a real Legistar record carries once a bill is actually enacted (the
# matter's own summary/title text often grows a line once history is final, and/or an
# attachment supplies it -- see monitor_records D8). Only used at the ADOPTED stage, to
# give the LLM extraction (Scenario B) genuine textual evidence of a final vote to be
# confident about -- exercising the real call_llm_extract() path honestly rather than
# feeding it text too thin to ever justify confidence, which the system prompt correctly
# refuses to do (D5: "do NOT infer adopted/passed unless explicit evidence of a final
# vote"). Deterministic stage resolution (D1) never depends on this text either way --
# it always comes from the MatterHistory `passed_flag`, not from what the LLM reads.
_MATTER_TEXT_ADOPTED = (
    _MATTER_TEXT + " The Council held a public hearing on June 3, 2026. On June 9, 2026, "
    "the ordinance was passed by a vote of 9-0 and signed into law."
)


def _matter(status: str, when: datetime, *, raw_text: str = _MATTER_TEXT) -> RawRecord:
    return RawRecord(
        source="demo_legistar",
        external_id="CB121214",
        document_type="matter",
        title="An ordinance imposing a temporary moratorium on new data centers",
        source_url="https://seattle.legistar.com/LegislationDetail.aspx?ID=CB121214",
        published_at=when,
        meeting_date=when,
        raw_text=raw_text,
        metadata={"matter_status": status, "matter_file": "CB 121214"},
    )


def _history(action: str, passed: bool, when: datetime, idx: int) -> RawRecord:
    return RawRecord(
        source="demo_legistar", external_id=f"CB121214-hist-{idx}", document_type="history_action",
        title=action, source_url=None, published_at=when, meeting_date=when, raw_text=action,
        metadata={"action_name": action, "passed_flag": passed},
    )


async def run_phase1() -> tuple[dict, dict]:
    """Ingests the bill through all three real stage transitions, exactly as three
    separate Legistar polls would arrive over time, using the REAL ingest_matter()
    pipeline throughout -- classify -> deterministic stage_resolver -> (LLM extraction,
    with the built-in heuristic fallback if it's unavailable) -> canonicalize.

    Returns two JSON views of the SAME final ADOPTED event, both produced by real
    ingest_matter() calls against the same bill:
      - `heuristic_event`: the state right after the three polls with `use_llm=False`
        -- confidence is the fixed 0.4 the heuristic fallback always uses (see
        monitor_records/ingest.py) -- Phase 1's honest stand-in for "the record confirms
        this really was adopted, but nothing here confirms subject or intent beyond a
        keyword match."
      - `llm_confirmed_event`: a fourth, real ingest_matter() call on the SAME final
        state with `use_llm=True`. Stage does not change (it's deterministic, from the
        structural "Passed" signal, same as before -- D1) but title/description/subject/
        confidence/evidence are now whatever the real LLM extraction returned (whichever
        provider call_llm_extract() selects -- see module docstring; the keyword
        heuristic otherwise -- ingest_matter() falls back automatically on any LLM
        failure, no code change needed either way).
    """
    engine = sa_create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=SAStaticPool)
    P1Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    source = DemoSource()

    print("=== Phase 1: public record -> structured event ===\n")

    source.matters["CB121214"] = [_matter("In Committee", datetime(2026, 6, 1)), _history("Introduced", False, datetime(2026, 6, 1), 1)]
    r1 = await ingest_matter(source, session, "CB121214", use_llm=False)
    print(f"[Jun 1]  ingest (heuristic) -> stage={r1.stage:<10} created={r1.created} stage_changed={r1.stage_changed}")

    source.matters["CB121214"] = [
        _matter("Heard in Committee", datetime(2026, 6, 1)),
        _history("Introduced", False, datetime(2026, 6, 1), 1),
        _history("Public Hearing held", False, datetime(2026, 6, 3), 2),
    ]
    r2 = await ingest_matter(source, session, "CB121214", use_llm=False)
    print(f"[Jun 3]  ingest (heuristic) -> stage={r2.stage:<10} created={r2.created} stage_changed={r2.stage_changed}")

    source.matters["CB121214"] = [
        _matter("Passed", datetime(2026, 6, 1), raw_text=_MATTER_TEXT_ADOPTED),
        _history("Introduced", False, datetime(2026, 6, 1), 1),
        _history("Public Hearing held", False, datetime(2026, 6, 3), 2),
        _history("Passed", True, datetime(2026, 6, 9), 3),
    ]
    r3 = await ingest_matter(source, session, "CB121214", use_llm=False)
    print(f"[Jun 9]  ingest (heuristic) -> stage={r3.stage:<10} created={r3.created} stage_changed={r3.stage_changed}")

    # ingest_matter() never commits (transaction control is the caller's, same as the
    # real monitor_records.db.get_session() context manager, which auto-commits on exit
    # -- this script is a caller too, and needs to commit at its own natural boundaries).
    # Committing here also expires the session by default (expire_on_commit=True), so
    # the next attribute/relationship access lazily reloads fresh from the DB.
    session.commit()

    n_events = session.query(Event).count()
    print(f"\n3 ingests, 3 documents, {n_events} canonical Event (dedup by bill number, not by document)\n")

    event_row = session.query(Event).filter_by(canonical_id=r3.canonical_id).one()
    print(f"heuristic ingest result: event_type={event_row.event_type.value} stage={event_row.stage.value} "
          f"confidence={event_row.confidence:.2f} subject={event_row.subject!r}\n")
    heuristic_event = _event_to_json(event_row)

    # A fourth, REAL ingest_matter() call on the same final state, this time with the LLM
    # path enabled. Same document, same structural "Passed" signal -> same deterministic
    # ADOPTED stage (D1); only the LLM-sourced fields can change.
    r4 = await ingest_matter(source, session, "CB121214", use_llm=True)
    print(f"[Jun 9]  re-ingest (use_llm=True, used_llm={r4.used_llm}) -> stage={r4.stage:<10} "
          f"stage_changed={r4.stage_changed}")

    session.commit()  # same reasoning as above -- commit, then lazy reload picks up the update
    print(f"LLM-confirmed result:    event_type={event_row.event_type.value} stage={event_row.stage.value} "
          f"confidence={event_row.confidence:.2f} subject={event_row.subject!r}\n")
    llm_confirmed_event = _event_to_json(event_row)

    return heuristic_event, llm_confirmed_event


def _print_result(label: str, result) -> None:
    print(f"--- {label} ---")
    print(f"decision: {result.decision}")
    if result.metric:
        print(f"metric:   {result.metric} = {result.score:.3f}")
        for name, c in result.physical_components.items():
            print(f"  {name:<10} score={c['score']:.2f}  basis: {c['basis']}")
    for r in result.reasons:
        print(f"reason:   {r}")
    print(f"government evidence: {len(result.government_evidence)} citation(s)")
    print()


async def run_phase2_and_3(heuristic_event: dict, llm_confirmed_event: dict) -> object:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    p2 = Session(engine)
    p3 = Session(engine)

    good = Site(label="SODO industrial", lat=GOOD_SITE[0], lng=GOOD_SITE[1],
                political_locality="Seattle", political_region="Washington")
    bad = Site(label="Duwamish floodplain", lat=BAD_SITE[0], lng=BAD_SITE[1],
               political_locality="Seattle", political_region="Washington")
    p2.add(good); p2.add(bad); p2.commit(); p2.refresh(good); p2.refresh(bad)

    settings = Settings(mireye_api_token="fake-token-for-demo")
    async with MireyeClient(settings=settings, transport=make_transport()) as client:
        print("=== Phase 2 + Phase 3: physical features -> materiality decision ===\n")

        print("Scenario A: the REAL ingest result with the LLM path deliberately disabled")
        print("(use_llm=False) -> confidence 0.4, the heuristic fallback's fixed value for")
        print("a keyword-only match. Same ADOPTED bill, same physically-strong site --")
        print("must still stay SILENCE.\n")
        heuristic_result = await decide(heuristic_event, good.id, p2_session=p2, p3_session=p3, client=client)
        _print_result("SODO industrial (GOOD_SITE), heuristic-confidence event", heuristic_result)

        print("Scenario B: the same bill, re-ingested with the real LLM extraction path")
        print("enabled (whichever provider call_llm_extract() selects -- OpenAI primary,")
        print("Gemini fallback; the keyword heuristic if neither key works). Now evaluated against")
        print("both sites to show the SAME event producing opposite decisions.\n")
        alert_result = await decide(llm_confirmed_event, good.id, p2_session=p2, p3_session=p3, client=client)
        _print_result("SODO industrial (GOOD_SITE) -- ALERT case", alert_result)

        silence_result = await decide(llm_confirmed_event, bad.id, p2_session=p2, p3_session=p3, client=client)
        _print_result("Duwamish floodplain (BAD_SITE) -- SILENCE case", silence_result)

        return alert_result, silence_result


async def main() -> None:
    heuristic_event, llm_confirmed_event = await run_phase1()
    alert_result, silence_result = await run_phase2_and_3(heuristic_event, llm_confirmed_event)

    print("=== full ALERT decision payload ===")
    print(json.dumps(alert_result.model_dump(mode="json"), indent=2, default=str))
    print()
    print("=== full SILENCE decision payload ===")
    print(json.dumps(silence_result.model_dump(mode="json"), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
