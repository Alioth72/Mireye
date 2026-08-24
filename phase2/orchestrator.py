"""Fetch orchestrator: quote -> fetch -> normalize -> store -> log.

This is the piece that was designed (context/phase2.md step 5) but never wired to HTTP on
the phase2 branch -- store.py, bundles.py, and mireye/client.py were all already correct
and tested, they just had no caller. Both site registration and the bundle-fetch endpoints
share this so there is exactly one place credits get spent and logged.
"""

from __future__ import annotations

from typing import Iterable

from sqlmodel import Session

from .bundles import estimate_credits
from .config import get_settings
from .mireye.client import MireyeClient
from .mireye.schemas import MireyeError
from .models import FetchLog, Site
from .store import upsert_records


async def fetch_and_store(
    session: Session,
    client: MireyeClient,
    site: Site,
    field_names: Iterable[str],
    *,
    trigger: str = "cache_miss",
    caller_ref: str | None = None,
) -> None:
    """Fetch `field_names` for `site` from Mireye, persist the results, and log the
    spend. Raises MireyeError on failure (nothing is silently swallowed -- callers
    decide whether a fetch failure should surface to the HTTP caller); the failure is
    still logged to FetchLog first so the credit audit trail stays complete either way.
    """
    fields = list(dict.fromkeys(field_names))
    if not fields:
        return

    settings = get_settings()
    quoted_credits = None
    credits_remaining = None
    if settings.phase2_quote_before_fetch:
        try:
            quote = await client.quote(fields=fields, locations=1)
            quoted_credits = quote.credits_total
            if quote.allowance is not None:
                credits_remaining = quote.allowance.credits_remaining
        except MireyeError:
            # Quoting is free but not load-bearing -- a quote failure must not block
            # the fetch itself. Local estimate still gets logged below.
            pass

    try:
        response, request_id = await client.fetch(lat=site.lat, lng=site.lng, fields=fields)
    except MireyeError as exc:
        session.add(
            FetchLog(
                site_id=site.id,
                fields=fields,
                quoted_credits=quoted_credits if quoted_credits is not None else estimate_credits(fields),
                credits_remaining=credits_remaining,
                trigger=trigger,
                caller_ref=caller_ref,
                ok=False,
                error=str(exc),
            )
        )
        session.commit()
        raise

    upsert_records(session, site.id, response.fields, request_id=request_id)

    session.add(
        FetchLog(
            site_id=site.id,
            fields=fields,
            quoted_credits=quoted_credits if quoted_credits is not None else estimate_credits(fields),
            # Mireye's /v1/fetch response carries no per-call credit total (see
            # mireye/schemas.py FetchResponse) -- the local field-count estimate is the
            # best available "charged" figure; the quote (when available) is authoritative
            # for what WILL be charged, logged separately as quoted_credits.
            charged_credits=estimate_credits(fields),
            credits_remaining=credits_remaining,
            request_id=request_id,
            trigger=trigger,
            caller_ref=caller_ref,
            ok=True,
        )
    )
    session.commit()
