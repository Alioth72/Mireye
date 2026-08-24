"""
Run one ingestion pass against real Seattle Legistar data and print results.

Usage:
    python scripts/run_ingest.py                       # discover+ingest everything since --days ago
    python scripts/run_ingest.py --days 30
    python scripts/run_ingest.py --matter-id 121279 --no-llm
    python scripts/run_ingest.py --matter-id 121279     # requires ANTHROPIC_API_KEY

Without ANTHROPIC_API_KEY set, extraction falls back to the keyword-based
event_type heuristic (classify.guess_event_type) -- stage resolution is
unaffected either way, since it's deterministic (stage_resolver.py) and
never depends on the LLM.
"""

from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()
import argparse
import asyncio
import json
from datetime import datetime, timedelta

from monitor_records.db import get_session, init_db
from monitor_records.ingest import ingest_matter, ingest_recent
from monitor_records.sources.seattle_legistar import BASE_URL, SeattleLegistarSource


async def _resolve_bill_to_matter_id(bill: str) -> str | None:
    """Look up the internal Legistar MatterId for a human bill number like
    'CB 121279' or 'CB121279' -- the --matter-id flag needs the MatterId
    (e.g. 17425), not the bill number, and that distinction is easy to trip
    over."""
    import httpx

    # normalize "CB121279" / "CB 121279" / "cb-121279" -> "CB 121279"
    normalized = bill.upper().replace("-", " ")
    if " " not in normalized:
        for prefix in ("CB", "RES", "ORD"):
            if normalized.startswith(prefix):
                normalized = f"{prefix} {normalized[len(prefix):]}"
                break

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/matters",
            params={"$filter": f"MatterFile eq '{normalized}'"},
        )
        resp.raise_for_status()
        matches = resp.json()
        if not matches:
            return None
        return str(matches[0]["MatterId"])


async def main(days: int, matter_id: str | None, bill: str | None, use_llm: bool) -> None:
    init_db()
    source = SeattleLegistarSource()
    try:
        if bill:
            resolved = await _resolve_bill_to_matter_id(bill)
            if resolved is None:
                print(f"No matter found with bill number '{bill}'")
                return
            print(f"Resolved bill '{bill}' -> MatterId {resolved}")
            matter_id = resolved

        with get_session() as session:
            if matter_id:
                result = await ingest_matter(source, session, matter_id, use_llm=use_llm)
                print(json.dumps(result.__dict__, indent=2))
            else:
                since = datetime.utcnow() - timedelta(days=days)
                results = await ingest_recent(source, session, since=since, use_llm=use_llm)
                for r in results:
                    print(json.dumps(r.__dict__))
                created = sum(1 for r in results if r.created)
                changed = sum(1 for r in results if r.stage_changed and not r.created)
                skipped = sum(1 for r in results if r.skipped_reason)
                print(
                    f"\n{len(results)} matters processed | "
                    f"{created} new events | {changed} stage updates | {skipped} skipped"
                )
    finally:
        await source.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14, help="lookback window when not using --matter-id/--bill")
    parser.add_argument("--matter-id", type=str, default=None, help="internal Legistar MatterId, e.g. 17425")
    parser.add_argument("--bill", type=str, default=None, help="human bill number, e.g. 'CB 121279' -- resolved to a MatterId for you")
    parser.add_argument("--no-llm", action="store_true", help="skip LLM extraction, use keyword heuristic only")
    args = parser.parse_args()

    asyncio.run(main(args.days, args.matter_id, args.bill, use_llm=not args.no_llm))