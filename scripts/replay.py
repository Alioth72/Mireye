"""
Historical replay.

Pulls Seattle Legistar matters introduced within [--start, --end], ingests
them in chronological order (by MatterIntroDate), and prints each stage
transition as it would have fired at the time. EventVersion.occurred_at is
set from the record's own dates (not ingestion wall-clock time), so replay
output reflects the real historical timeline -- this is what lets the
downstream monitor/Mireye team evaluate lead time (how much warning would
this system have given, had it been running back then).

Usage:
    python scripts/replay.py --start 2026-01-01 --end 2026-07-01
    python scripts/replay.py --start 2026-01-01 --end 2026-07-01 --no-llm
"""

from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()
import argparse
import asyncio
from datetime import datetime

import httpx

from monitor_records.db import get_session, init_db
from monitor_records.ingest import ingest_matter
from monitor_records.sources.seattle_legistar import (
    BASE_URL,
    RELEVANT_MATTER_TYPES,
    SeattleLegistarSource,
)


async def _matters_in_range(start: datetime, end: datetime) -> list[str]:
    """List MatterIds introduced in [start, end), ordered chronologically,
    restricted to substantive legislation types."""
    async with httpx.AsyncClient(timeout=30) as client:
        start_str = start.strftime("%Y-%m-%dT%H:%M:%S")
        end_str = end.strftime("%Y-%m-%dT%H:%M:%S")
        resp = await client.get(
            f"{BASE_URL}/matters",
            params={
                "$filter": (
                    f"MatterIntroDate ge datetime'{start_str}' "
                    f"and MatterIntroDate lt datetime'{end_str}'"
                ),
                "$orderby": "MatterIntroDate asc",
                "$top": "1000",
            },
        )
        resp.raise_for_status()
        matters = resp.json()
        return [
            str(m["MatterId"])
            for m in matters
            if m.get("MatterTypeName") in RELEVANT_MATTER_TYPES
        ]


async def main(start: datetime, end: datetime, use_llm: bool) -> None:
    init_db()
    ids = await _matters_in_range(start, end)
    print(f"{len(ids)} matters to replay between {start.date()} and {end.date()}\n")

    source = SeattleLegistarSource()
    try:
        with get_session() as session:
            for external_id in ids:
                result = await ingest_matter(source, session, external_id, use_llm=use_llm)
                if result.skipped_reason:
                    continue
                marker = "NEW EVENT" if result.created else ("STAGE CHANGE" if result.stage_changed else "no-op")
                print(f"[{marker}] {external_id} -> {result.stage} (canonical_id={result.canonical_id})")
    finally:
        await source.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()

    asyncio.run(
        main(
            datetime.strptime(args.start, "%Y-%m-%d"),
            datetime.strptime(args.end, "%Y-%m-%d"),
            use_llm=not args.no_llm,
        )
    )
