"""Executor — the only agent that touches Mireye, and it has no model.

Every fetch and every score runs here, deterministically. That separation is the point:
agents choose *what* to investigate, this executes it, and the numbers stay reproducible.
An LLM anywhere in this path would make the same inputs produce different outputs, which
would quietly destroy the weight calibration the scores exist to support.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlmodel import Session, select

from .. import scoring
from ..models import Datapoint, VicinitySummary
from ..mireye.client import MireyeClient
from ..orchestrator import read_or_fetch, scan_vicinity
from .base import Investigation


class Executor:
    """Fetches and scores. Decides nothing."""

    name = "executor"
    model_tier = None   # deterministic by design -- see module docstring

    def __init__(self, session: Session, client: MireyeClient):
        self.session = session
        self.client = client

    # -- reading state ------------------------------------------------------
    def datapoints(self, site_id: str) -> list:
        return self.session.exec(
            select(Datapoint).where(Datapoint.site_id == site_id)
        ).all()

    def vicinity_map(self, site_id: str) -> dict:
        rows = self.session.exec(
            select(VicinitySummary).where(VicinitySummary.site_id == site_id)
        ).all()
        return {
            r.field_name: {
                "best": r.best, "worst": r.worst, "best_at_m": r.best_at_m,
                "spread": r.spread, "fraction_usable": r.fraction_usable,
                "n_answers": r.n_answers, "n_with_value": r.n_with_value,
                "coverage_note": r.coverage_note, "class": r.field_class,
            }
            for r in rows
        }

    def observed(self, site_id: str) -> dict:
        """Field -> {value, status}, the shape the Scout's rules read."""
        return {dp.field_name: {"value": dp.value, "status": dp.status}
                for dp in self.datapoints(site_id)}

    # -- acting -------------------------------------------------------------
    async def scan(self, inv: Investigation, fields: list, *, site) -> Investigation:
        """One vicinity scan: 25 locations, one batch call."""
        if not inv.budget.can_fetch():
            inv.record(self.name, "fetch_skipped", "fetch budget exhausted")
            return inv

        inv.budget.fetches_used += 1
        out = await scan_vicinity(self.session, self.client, site, fields,
                                  caller_ref=f"agent:{inv.site_id}")
        if out.get("error"):
            inv.record(self.name, "vicinity_scan_failed", out["error"])
            return inv

        for f in fields:
            if f not in inv.fields_fetched:
                inv.fields_fetched.append(f)
            inv.resolve(f"fetch {f}")

        inv.vicinity = self.vicinity_map(inv.site_id)
        inv.record(
            self.name, "vicinity_scan",
            f"sampled {out['points_returned']} ring points across {len(fields)} fields",
            result={"points": out["points_returned"], "fields": len(fields),
                    "rings": out["rings"]},
        )
        return inv

    async def top_up(self, inv: Investigation, fields: list, *, site) -> Investigation:
        """Fetch a handful of follow-up fields at the centroid.

        Deliberately a point fetch, not a ring: the Scout's follow-ups are qualifiers
        about THIS ground (GAP status, karst exposure, flood zone), and a qualifier
        found 1.5 km away describes someone else's parcel.
        """
        if not fields:
            return inv
        if not inv.budget.can_fetch():
            inv.record(self.name, "top_up_skipped", "fetch budget exhausted")
            return inv

        inv.budget.fetches_used += 1
        answers, outcome = await read_or_fetch(
            self.session, self.client, site, fields, trigger="cache_miss",
            caller_ref=f"agent:{inv.site_id}",
        )
        for f in fields:
            if f not in inv.fields_fetched:
                inv.fields_fetched.append(f)
            inv.resolve(f"fetch {f}")

        inv.record(
            self.name, "qualifier_fetch",
            "fetched Scout-requested qualifiers at the centroid (these describe THIS "
            "ground, so a ring would answer about a neighbour's parcel)",
            result={"fields": fields, "warnings": outcome.warnings},
        )
        return inv

    def score(self, inv: Investigation, metrics: Optional[list] = None) -> Investigation:
        """Recompute every metric from stored facts. Pure function of the datapoints."""
        dps = self.datapoints(inv.site_id)
        vic = self.vicinity_map(inv.site_id) or None
        for metric in (metrics or [inv.goal]):
            inv.scores[metric] = scoring.score(metric, dps, vicinity=vic)
        inv.record(
            self.name, "score",
            "deterministic recompute over stored facts",
            result={m: r["score"] for m, r in inv.scores.items()},
        )
        return inv

    async def run(self, inv: Investigation, **kwargs: Any) -> Investigation:
        return self.score(inv)
