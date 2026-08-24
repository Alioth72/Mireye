"""
Seattle Legistar adapter.

The Legistar Web API is a documented, unauthenticated (for Seattle), read-only
OData-ish REST API: https://webapi.legistar.com/v1/seattle/...

Key endpoints used:
  GET /matters                       -- list bills/resolutions ("Matters")
  GET /matters/{id}/histories        -- recorded actions taken on a Matter
                                         (this is our deterministic stage signal)
  GET /matters/{id}/attachments       -- staff reports / amendments / etc.

We deliberately do NOT scrape the Legistar HTML site. The API gives us
structured status fields (MatterStatusName, MatterPassedDate,
MatterEnactmentDate) and a real action history, which is what lets stage
resolution be rule-based instead of LLM-inferred, per the spec.

NOTE: this module makes real network calls. It's not exercised by the unit
tests (see tests/), which use canned fixtures instead. Run
scripts/probe_legistar.py locally to sanity check against the live API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from .base import RawRecord, RecordSource

BASE_URL = "https://webapi.legistar.com/v1/seattle"

# Legistar's Matters list includes a lot of purely procedural record types
# (Introduction & Referral Calendars, Minutes, Information Items) that are
# never themselves the legislation we care about -- e.g. an IRC can show
# MatterStatusName="Adopted" for "the weekly calendar was adopted", which is
# NOT a rezoning/moratorium/etc being enacted. Confirmed against live Seattle
# data on 2026-08-23 (IRC 536, Min 580 both showed up with misleading status
# strings). Restrict discover() to substantive legislation types.
RELEVANT_MATTER_TYPES = {
    "Council Bill (CB)",
    "Resolution (Res)",
    "Ordinance (Ord)",
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class SeattleLegistarSource(RecordSource):
    name = "seattle_legistar"

    def __init__(self, client: httpx.AsyncClient | None = None, timeout: float = 30.0):
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def discover(self, since: datetime | None = None) -> list[str]:
        """List MatterIds introduced/modified since `since`.

        Uses MatterLastModifiedUtc so re-runs pick up status changes
        (e.g. PROPOSED -> ADOPTED) on matters we've already seen, not just
        brand-new matters.
        """
        client = await self._get_client()
        params: dict[str, Any] = {
            "$orderby": "MatterLastModifiedUtc desc",
            "$top": "1000",
        }
        if since is not None:
            since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
            params["$filter"] = f"MatterLastModifiedUtc ge datetime'{since_str}'"

        resp = await client.get(f"{BASE_URL}/matters", params=params)
        resp.raise_for_status()
        matters = resp.json()
        return [
            str(m["MatterId"])
            for m in matters
            if m.get("MatterTypeName") in RELEVANT_MATTER_TYPES
        ]

    async def fetch(self, external_id: str) -> list[RawRecord]:
        client = await self._get_client()
        records: list[RawRecord] = []

        matter_resp = await client.get(f"{BASE_URL}/matters/{external_id}")
        matter_resp.raise_for_status()
        matter = matter_resp.json()

        matter_url = f"https://seattle.legistar.com/LegislationDetail.aspx?ID={external_id}"

        records.append(
            RawRecord(
                source=self.name,
                external_id=external_id,
                document_type="matter",
                title=matter.get("MatterTitle") or matter.get("MatterName"),
                source_url=matter_url,
                published_at=_parse_dt(matter.get("MatterIntroDate")),
                meeting_date=_parse_dt(matter.get("MatterAgendaDate")),
                raw_text=matter.get("MatterTitle"),
                metadata={
                    "matter_file": matter.get("MatterFile"),  # e.g. "CB 121214"
                    "matter_type": matter.get("MatterTypeName"),
                    "matter_status": matter.get("MatterStatusName"),
                    "matter_body": matter.get("MatterBodyName"),
                    "intro_date": matter.get("MatterIntroDate"),
                    "agenda_date": matter.get("MatterAgendaDate"),
                    "passed_date": matter.get("MatterPassedDate"),
                    "enactment_date": matter.get("MatterEnactmentDate"),
                    "enactment_number": matter.get("MatterEnactmentNumber"),
                },
            )
        )

        hist_resp = await client.get(f"{BASE_URL}/matters/{external_id}/histories")
        if hist_resp.status_code == 200:
            for h in hist_resp.json():
                records.append(
                    RawRecord(
                        source=self.name,
                        external_id=f"{external_id}-hist-{h.get('MatterHistoryId')}",
                        document_type="history_action",
                        title=h.get("MatterHistoryActionName"),
                        source_url=matter_url,
                        published_at=_parse_dt(h.get("MatterHistoryActionDate")),
                        meeting_date=_parse_dt(h.get("MatterHistoryActionDate")),
                        raw_text=h.get("MatterHistoryActionName"),
                        metadata={
                            "parent_matter_id": external_id,
                            "action_body_name": h.get("MatterHistoryActionBodyName"),
                            "action_name": h.get("MatterHistoryActionName"),
                            "passed_flag": h.get("MatterHistoryPassedFlag"),
                            "action_date": h.get("MatterHistoryActionDate"),
                        },
                    )
                )

        attach_resp = await client.get(f"{BASE_URL}/matters/{external_id}/attachments")
        if attach_resp.status_code == 200:
            for a in attach_resp.json():
                records.append(
                    RawRecord(
                        source=self.name,
                        external_id=f"{external_id}-attach-{a.get('MatterAttachmentId')}",
                        document_type="attachment",
                        title=a.get("MatterAttachmentName"),
                        source_url=a.get("MatterAttachmentHyperlink"),
                        published_at=None,
                        meeting_date=None,
                        # Text extraction from the attachment binary (often PDF,
                        # sometimes scanned) is intentionally NOT done here --
                        # see risk #1 in the design notes. Wire in the `pdf`
                        # extraction step separately once you've checked a
                        # real sample.
                        raw_text=None,
                        metadata={
                            "parent_matter_id": external_id,
                            "attachment_name": a.get("MatterAttachmentName"),
                        },
                    )
                )

        return records
