"""Seed the persistent console databases with a free, deterministic demo.

Unlike ``run_pipeline.py`` (an isolated in-memory proof), this writes to the
same Phase 1/2/3 databases read by the combined API. The Mireye HTTP boundary
uses the repository's fake transport, so it never spends credits.

Safe to run repeatedly. Usage, from backend/::

    python scripts/seed_demo.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

load_dotenv(REPO_DIR / ".env")
load_dotenv(BACKEND_DIR / ".env", override=True)

from sqlmodel import Session, select  # noqa: E402

from monitor_records.api import _event_to_json  # noqa: E402
from monitor_records.db import SessionLocal, init_db as init_p1_db  # noqa: E402
from monitor_records.ingest import ingest_matter  # noqa: E402
from monitor_records.models import Document, Event, Evidence  # noqa: E402
from phase2.config import Settings  # noqa: E402
from phase2.db import get_engine as get_p2_engine, init_db as init_p2_db  # noqa: E402
from phase2.mireye.client import MireyeClient  # noqa: E402
from phase2.models import Site  # noqa: E402
from phase3.db import get_engine as get_p3_engine, init_db as init_p3_db  # noqa: E402
from phase3.pipeline import decide  # noqa: E402
from scripts.run_pipeline import (  # noqa: E402
    DemoSource,
    _MATTER_TEXT_ADOPTED,
    _history,
    _matter,
)
from tests.fakes.mireye_fake import BAD_SITE, GOOD_SITE, make_transport  # noqa: E402

CANONICAL_ID = "seattle:demo-legistar:cb-121214"


async def seed_event() -> dict:
    init_p1_db()
    session = SessionLocal()
    try:
        event = session.query(Event).filter_by(canonical_id=CANONICAL_ID).one_or_none()
        if event is None:
            source = DemoSource()
            source.matters["CB121214"] = [
                _matter("In Committee", datetime(2026, 6, 1)),
                _history("Introduced", False, datetime(2026, 6, 1), 1),
            ]
            await ingest_matter(source, session, "CB121214", use_llm=False)

            source.matters["CB121214"] = [
                _matter("Heard in Committee", datetime(2026, 6, 1)),
                _history("Introduced", False, datetime(2026, 6, 1), 1),
                _history("Public Hearing held", False, datetime(2026, 6, 3), 2),
            ]
            await ingest_matter(source, session, "CB121214", use_llm=False)

            source.matters["CB121214"] = [
                _matter("Passed", datetime(2026, 6, 1), raw_text=_MATTER_TEXT_ADOPTED),
                _history("Introduced", False, datetime(2026, 6, 1), 1),
                _history("Public Hearing held", False, datetime(2026, 6, 3), 2),
                _history("Passed", True, datetime(2026, 6, 9), 3),
            ]
            result = await ingest_matter(source, session, "CB121214", use_llm=False)
            event = session.query(Event).filter_by(canonical_id=result.canonical_id).one()

        # The heuristic caps confidence at 0.4. This confirmed demo fixture must
        # exercise the physical gate, so its reviewed fields are explicit.
        event.title = "Temporary moratorium on new data centers"
        event.description = (
            "Seattle adopted a temporary citywide moratorium on new data-center "
            "development after a public hearing and a 9-0 final vote."
        )
        event.subject = "data centers"
        event.confidence = 0.91

        matter = session.query(Document).filter_by(
            source="demo_legistar", external_id="CB121214"
        ).one()
        evidence = session.query(Evidence).filter_by(
            event_id=event.id,
            reason="Demo record: final passage and scope",
        ).one_or_none()
        if evidence is None:
            session.add(
                Evidence(
                    event_id=event.id,
                    document_id=matter.id,
                    passage="The ordinance was passed by a vote of 9-0 and signed into law.",
                    reason="Demo record: final passage and scope",
                    url=matter.source_url,
                )
            )

        session.commit()
        session.refresh(event)
        return _event_to_json(event)
    finally:
        session.close()


def get_or_create_site(session: Session, *, label: str, coords: tuple[float, float]) -> Site:
    site = session.exec(select(Site).where(Site.label == label)).first()
    if site is None:
        site = Site(
            label=label,
            lat=coords[0],
            lng=coords[1],
            political_locality="Seattle",
            political_county="King County",
            political_region="Washington",
        )
        session.add(site)
        session.commit()
        session.refresh(site)
    return site


async def seed_physical_and_decisions(event: dict) -> list[tuple[str, str]]:
    init_p2_db()
    init_p3_db()
    results: list[tuple[str, str]] = []

    with Session(get_p2_engine()) as p2, Session(get_p3_engine()) as p3:
        sites = [
            get_or_create_site(p2, label="SODO industrial", coords=GOOD_SITE),
            get_or_create_site(p2, label="Duwamish floodplain", coords=BAD_SITE),
        ]

        fake_settings = Settings(
            mireye_api_token="fake-token-for-local-demo",
            mireye_base_url="https://demo.invalid",
        )
        async with MireyeClient(settings=fake_settings, transport=make_transport()) as client:
            for site in sites:
                result = await decide(
                    event,
                    site.id,
                    p2_session=p2,
                    p3_session=p3,
                    client=client,
                )
                results.append((site.label or site.id, result.decision))
    return results


async def main() -> None:
    event = await seed_event()
    results = await seed_physical_and_decisions(event)

    print("Persistent demo data is ready.")
    print(f"Event: {event['title']} ({event['stage']}, confidence {event['confidence']:.2f})")
    for label, decision in results:
        print(f"Site:  {label} -> {decision}")
    print("Refresh http://127.0.0.1:8080 with Ctrl+Shift+R.")


if __name__ == "__main__":
    asyncio.run(main())
