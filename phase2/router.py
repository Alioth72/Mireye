"""Phase 2 HTTP surface.

Everything hangs off a single ``APIRouter`` with no module-level app object, so the
post-merge integration into Phase 3 is ``app.include_router(phase2.router)`` and
nothing else changes.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session

from . import catalog
from .bundles import (
    BUNDLES,
    MAX_FIELDS_PER_FETCH,
    UnknownBundle,
    bundle_fields,
    estimate_credits,
    fields_for,
    touches_parcel_record,
)
from .config import get_settings
from .db import get_session
from .mireye.client import MireyeClient
from .mireye.schemas import MireyeError

router = APIRouter(prefix="/v1", tags=["phase2"])


# --------------------------------------------------------------------------
# dependencies
# --------------------------------------------------------------------------
async def get_client() -> AsyncIterator[MireyeClient]:
    client = MireyeClient()
    async with client:
        yield client


def _http_error(exc: MireyeError) -> HTTPException:
    """Surface Mireye's error shape rather than flattening it to a 500."""
    status = exc.status_code or (503 if exc.retryable else 500)
    return HTTPException(
        status_code=status,
        detail={
            "error": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "request_id": exc.request_id,
            "retry_after": exc.retry_after,
        },
    )


# --------------------------------------------------------------------------
# health & catalog
# --------------------------------------------------------------------------
@router.get("/healthz", tags=["ops"])
def healthz() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "token_configured": bool(settings.mireye_api_token),
        "autofetch_on_miss": settings.phase2_autofetch_on_miss,
        "quote_before_fetch": settings.phase2_quote_before_fetch,
    }


@router.get("/catalog/fields", tags=["ops"])
async def catalog_fields(
    force: bool = Query(False, description="Bypass the local TTL and re-check the ETag"),
    session: Session = Depends(get_session),
    client: MireyeClient = Depends(get_client),
) -> dict[str, Any]:
    try:
        row = await catalog.refresh(session, client, force=force)
    except MireyeError as exc:
        raise _http_error(exc) from exc
    names = catalog._extract_field_names(row.payload)
    return {
        "etag": row.etag,
        "fetched_at": row.fetched_at,
        "field_count": len(names),
        "fields": sorted(names),
    }


@router.get("/bundles", tags=["ops"])
def list_bundles(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Bundle definitions plus a local credit estimate. Authority on price is /v1/quote."""
    known = catalog.known_field_names(session)
    return {
        "bundles": {
            name: {
                "fields": list(fields),
                "estimated_credits": estimate_credits(fields),
                "unknown_fields": sorted(f for f in fields if known and f not in known),
                "touches_parcel_record": sorted(touches_parcel_record(fields)),
            }
            for name, fields in BUNDLES.items()
        },
        "catalog_mirrored": bool(known),
    }


# --------------------------------------------------------------------------
# quote & budget
# --------------------------------------------------------------------------
class QuoteRequest(BaseModel):
    bundles: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    locations: int = 1


@router.post("/quote", tags=["ops"])
async def quote(
    body: QuoteRequest,
    client: MireyeClient = Depends(get_client),
) -> dict[str, Any]:
    """Free, unmetered, exact -- computed by the same code that charges.

    Accepts bundle names, explicit fields, or both.
    """
    try:
        selection = fields_for(body.bundles) if body.bundles else []
    except UnknownBundle as exc:
        raise HTTPException(status_code=400, detail={"error": "unknown_bundle", "message": str(exc)})

    for field in body.fields:
        if field not in selection:
            selection.append(field)

    if not selection:
        raise HTTPException(
            status_code=400,
            detail={"error": "no_fields_requested", "message": "send bundles and/or fields"},
        )
    if len(selection) > MAX_FIELDS_PER_FETCH:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "fields_too_many",
                "message": f"{len(selection)} fields exceeds the {MAX_FIELDS_PER_FETCH} cap",
            },
        )

    parcel_hits = touches_parcel_record(selection)
    try:
        remote = await client.quote(fields=selection, locations=body.locations)
    except MireyeError as exc:
        raise _http_error(exc) from exc

    return {
        "fields": selection,
        "locations": body.locations,
        "local_estimate_credits": estimate_credits(selection) * body.locations,
        "touches_parcel_record": sorted(parcel_hits),
        "quote": remote.model_dump(mode="json"),
    }


@router.get("/budget", tags=["ops"])
async def budget(client: MireyeClient = Depends(get_client)) -> dict[str, Any]:
    """Informational. There is no budget gate -- see ideation sec. 2 'On credits'."""
    try:
        return await client.usage()
    except MireyeError as exc:
        raise _http_error(exc) from exc
