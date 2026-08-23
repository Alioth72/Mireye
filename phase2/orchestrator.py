"""Fetch orchestration: quote -> fetch -> verify -> store -> log.

This is the only place in Phase 2 that spends credits. Everything else reads the
datapoint store.

Guardrails here are deliberately NOT budget gates (see ideation D5/R2 -- the team
decided credits are not scarce). They exist to stop a runaway loop, not to stop
spending:

* **In-flight deduplication** -- concurrent identical fetches for one site share a
  single upstream call rather than each opening their own.
* **Negative caching** of failed fields is handled in `store.upsert_records`.
* **A loud warning** when one site is fetched more times per hour than expected.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import timedelta
from typing import Optional

from sqlmodel import Session, select

from . import catalog
from .bundles import MAX_FIELDS_PER_FETCH, estimate_credits, touches_parcel_record
from .config import Settings, get_settings
from .models import Datapoint, FetchLog, Site, utcnow
from .mireye.client import MireyeClient
from .mireye.schemas import MireyeError
from .store import _aware, read_fields, upsert_records

log = logging.getLogger("phase2.orchestrator")

#: One lock per (site_id, field-set) so concurrent identical requests share a fetch.
_inflight: dict[tuple[str, str], asyncio.Lock] = {}


def _inflight_key(site_id: str, fields: list[str]) -> tuple[str, str]:
    return site_id, ",".join(sorted(fields))


@dataclass
class FetchOutcome:
    """What a fetch attempt actually did. Cost is always visible to the caller."""

    fetched: list[str] = dc_field(default_factory=list)
    quoted_credits: Optional[int] = None
    charged_credits: Optional[int] = None
    credits_remaining: Optional[int] = None
    request_id: Optional[str] = None
    resolved_location_ok: Optional[bool] = None
    warnings: list[str] = dc_field(default_factory=list)
    error: Optional[str] = None


def _recent_fetch_count(session: Session, site_id: str) -> int:
    cutoff = utcnow() - timedelta(hours=1)
    rows = session.exec(select(FetchLog).where(FetchLog.site_id == site_id)).all()
    return sum(1 for r in rows if (_aware(r.created_at) or utcnow()) >= cutoff)


def _location_matches(site: Site, resolved) -> bool:
    """A wrong-place answer is only catchable if the place is stated -- so check it.

    Tolerance is deliberately loose (~100 m); we are catching a different-place bug,
    not measuring precision.
    """
    if resolved is None:
        return True  # nothing to check against; do not invent a failure
    return abs(resolved.lat - site.lat) < 0.001 and abs(resolved.lng - site.lng) < 0.001


async def fetch_fields(
    session: Session,
    client: MireyeClient,
    site: Site,
    fields: list[str],
    *,
    trigger: str = "cache_miss",
    caller_ref: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> FetchOutcome:
    """Fetch `fields` for `site` and persist them. Returns what it cost."""
    settings = settings or get_settings()
    outcome = FetchOutcome(fetched=list(fields))

    if not fields:
        return outcome

    if len(fields) > MAX_FIELDS_PER_FETCH:
        outcome.error = f"fields_too_many: {len(fields)} > {MAX_FIELDS_PER_FETCH}"
        return outcome

    # Refuse the 300-credit cliff outright. The brief puts parcel fields out of scope,
    # and with no budget gate this check and the bundle lint are the only backstops.
    parcel_hits = touches_parcel_record(fields)
    if parcel_hits:
        outcome.error = f"refused: would trigger parcel_record group {sorted(parcel_hits)}"
        return outcome

    unknown = catalog.unknown_fields(session, fields)
    if unknown:
        outcome.warnings.append(f"not in mirrored catalog: {sorted(unknown)}")

    burst = _recent_fetch_count(session, site.id)
    if burst >= settings.phase2_fetch_warn_per_site_per_hour:
        msg = f"site {site.id} fetched {burst}x in the last hour -- possible runaway loop"
        log.warning(msg)
        outcome.warnings.append(msg)

    lock = _inflight.setdefault(_inflight_key(site.id, fields), asyncio.Lock())
    async with lock:
        # Another coroutine may have satisfied this while we waited on the lock.
        still_needed = read_fields(session, site.id, fields, settings=settings).to_fetch
        if not still_needed:
            outcome.fetched = []
            outcome.warnings.append("served by a concurrent fetch")
            return outcome

        if settings.phase2_quote_before_fetch:
            try:
                quote = await client.quote(fields=still_needed, locations=1)
                outcome.quoted_credits = quote.credits_total
                if quote.allowance:
                    outcome.credits_remaining = quote.allowance.credits_remaining
                    if quote.allowance.would_be_blocked:
                        outcome.warnings.append("quote says this request would be blocked")
            except MireyeError as exc:
                outcome.warnings.append(f"quote failed ({exc.code}); proceeding")

        try:
            response, request_id = await client.fetch(
                lat=site.lat,
                lng=site.lng,
                fields=still_needed,
                idempotency_key=str(uuid.uuid4()),
            )
        except MireyeError as exc:
            outcome.error = f"{exc.code}: {exc.message}"
            session.add(
                FetchLog(
                    site_id=site.id,
                    fields=still_needed,
                    quoted_credits=outcome.quoted_credits,
                    trigger=trigger,
                    caller_ref=caller_ref,
                    ok=False,
                    error=outcome.error,
                    request_id=exc.request_id,
                )
            )
            session.commit()
            return outcome

        outcome.request_id = request_id
        outcome.fetched = still_needed

        outcome.resolved_location_ok = _location_matches(site, response.resolved_location)
        if outcome.resolved_location_ok is False:
            rl = response.resolved_location
            outcome.warnings.append(
                f"resolved_location ({rl.lat}, {rl.lng}) does not match site "
                f"({site.lat}, {site.lng}) -- possible wrong-place answer"
            )

        if response.partial_failures:
            outcome.warnings.append(f"partial_failures: {len(response.partial_failures)}")

        upsert_records(session, site.id, response.fields, request_id=request_id)

        # Failed fields are refunded automatically, so charge is the non-failed count.
        charged = sum(1 for r in response.fields.values() if r.status != "failed")
        outcome.charged_credits = charged

        session.add(
            FetchLog(
                site_id=site.id,
                fields=still_needed,
                quoted_credits=outcome.quoted_credits,
                charged_credits=charged,
                credits_remaining=outcome.credits_remaining,
                request_id=request_id,
                trigger=trigger,
                caller_ref=caller_ref,
                ok=True,
            )
        )
        session.commit()

    return outcome


async def read_or_fetch(
    session: Session,
    client: MireyeClient,
    site: Site,
    fields: list[str],
    *,
    trigger: str = "cache_miss",
    caller_ref: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> tuple[list[Datapoint], FetchOutcome]:
    """Serve `fields` from cache, fetching whatever is missing or stale (D5).

    Returns the datapoints that carry real answers, plus what the fetch cost.
    """
    settings = settings or get_settings()
    read = read_fields(session, site.id, fields, settings=settings)
    outcome = FetchOutcome()

    if read.to_fetch and settings.phase2_autofetch_on_miss:
        outcome = await fetch_fields(
            session,
            client,
            site,
            read.to_fetch,
            trigger=trigger,
            caller_ref=caller_ref,
            settings=settings,
        )
        read = read_fields(session, site.id, fields, settings=settings)

    if read.withheld:
        outcome.warnings.append(
            f"withheld (failed, in negative-cache window): "
            f"{[dp.field_name for dp in read.withheld]}"
        )

    return read.answers, outcome
