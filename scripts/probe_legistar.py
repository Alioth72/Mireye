"""
Run this LOCALLY (this sandbox has no network access to webapi.legistar.com)
to validate the core assumption the whole design rests on: that Legistar's
MatterStatusName / MatterHistory give us reliable deterministic stage
signals for real Seattle bills.

Usage:
    python scripts/probe_legistar.py                     # recent matters
    python scripts/probe_legistar.py --matter-id 121214   # one specific matter
    python scripts/probe_legistar.py --search "data center"

What to look for:
  - Real MatterStatusName strings -> expand stage_resolver._STATUS_MAP
  - Real MatterHistoryActionName strings -> expand stage_resolver._ACTION_MAP
  - Whether MatterFile is reliably populated (canonical_id depends on it)
  - Whether attachments are text-extractable or scanned images (risk #1)
"""

from __future__ import annotations

import argparse
import asyncio
import json

import httpx

BASE_URL = "https://webapi.legistar.com/v1/seattle"


async def probe_recent(n: int = 10) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/matters",
            params={"$orderby": "MatterIntroDate desc", "$top": str(n)},
        )
        resp.raise_for_status()
        matters = resp.json()
        for m in matters:
            print(
                json.dumps(
                    {
                        "MatterId": m.get("MatterId"),
                        "MatterFile": m.get("MatterFile"),
                        "MatterTitle": (m.get("MatterTitle") or "")[:120],
                        "MatterTypeName": m.get("MatterTypeName"),
                        "MatterStatusName": m.get("MatterStatusName"),
                        "MatterIntroDate": m.get("MatterIntroDate"),
                        "MatterPassedDate": m.get("MatterPassedDate"),
                    }
                )
            )


async def probe_matter(matter_id: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        matter_resp = await client.get(f"{BASE_URL}/matters/{matter_id}")
        matter_resp.raise_for_status()
        print("--- MATTER ---")
        print(json.dumps(matter_resp.json(), indent=2))

        hist_resp = await client.get(f"{BASE_URL}/matters/{matter_id}/histories")
        print("\n--- HISTORY ---")
        if hist_resp.status_code == 200:
            for h in hist_resp.json():
                print(
                    json.dumps(
                        {
                            "ActionName": h.get("MatterHistoryActionName"),
                            "ActionBody": h.get("MatterHistoryActionBodyName"),
                            "PassedFlag": h.get("MatterHistoryPassedFlag"),
                            "ActionDate": h.get("MatterHistoryActionDate"),
                        }
                    )
                )
        else:
            print(f"(status {hist_resp.status_code})")

        attach_resp = await client.get(f"{BASE_URL}/matters/{matter_id}/attachments")
        print("\n--- ATTACHMENTS ---")
        if attach_resp.status_code == 200:
            for a in attach_resp.json():
                print(a.get("MatterAttachmentName"), "->", a.get("MatterAttachmentHyperlink"))


async def probe_search(text: str, n: int = 10) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/matters",
            params={
                "$filter": f"substringof('{text}', MatterTitle)",
                "$top": str(n),
            },
        )
        resp.raise_for_status()
        for m in resp.json():
            print(m.get("MatterId"), m.get("MatterFile"), m.get("MatterTitle"))


async def probe_types() -> None:
    """List distinct MatterTypeName values seen recently -- use this to
    confirm the exact strings RELEVANT_MATTER_TYPES should filter on."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/matters",
            params={"$orderby": "MatterIntroDate desc", "$top": "200"},
        )
        resp.raise_for_status()
        types = sorted({m.get("MatterTypeName") for m in resp.json()})
        for t in types:
            print(t)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--matter-id", type=str, default=None)
    parser.add_argument("--search", type=str, default=None)
    parser.add_argument("--types", action="store_true", help="list distinct MatterTypeName values")
    parser.add_argument("-n", type=int, default=10)
    args = parser.parse_args()

    if args.types:
        asyncio.run(probe_types())
    elif args.matter_id:
        asyncio.run(probe_matter(args.matter_id))
    elif args.search:
        asyncio.run(probe_search(args.search, args.n))
    else:
        asyncio.run(probe_recent(args.n))
