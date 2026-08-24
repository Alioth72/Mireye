"""Phase 2 HTTP surface.

Everything hangs off a single ``APIRouter`` with no module-level app object, so the
post-merge integration into Phase 3 is ``app.include_router(phase2.router)`` and
nothing else changes.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

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
from .models import FetchLog, Site, utcnow
from .orchestrator import fetch_and_store
from .store import read_fields, serialize

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


# --------------------------------------------------------------------------
# sites
# --------------------------------------------------------------------------
class SiteCreate(BaseModel):
    label: Optional[str] = None
    address: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


def _site_json(site: Site) -> dict[str, Any]:
    return {
        "id": site.id,
        "label": site.label,
        "address_raw": site.address_raw,
        "lat": site.lat,
        "lng": site.lng,
        "geocode_accuracy": site.geocode_accuracy,
        "accuracy_type": site.accuracy_type,
        "match_type": site.match_type,
        "normalized_address": site.normalized_address,
        "geocode_provider": site.geocode_provider,
        "parcel_grade": site.parcel_grade,
        "degraded": site.degraded,
        "precision_note": site.precision_note,
        "political_region": site.political_region,
        "political_county": site.political_county,
        "political_locality": site.political_locality,
        "tract_geoid": site.tract_geoid,
        "created_at": site.created_at,
    }


async def _apply_boundaries(session: Session, client: MireyeClient, site: Site) -> None:
    """Pull the `boundaries` bundle once at registration -- Census TIGER, ~1yr TTL, no
    shapefiles needed. Best-effort: a boundaries fetch failure must not block site
    creation, it just leaves scope-resolution fields empty for Phase 3 to see as unknown.
    """
    try:
        await fetch_and_store(session, client, site, bundle_fields("boundaries"), trigger="registration")
    except MireyeError:
        return
    read = read_fields(session, site.id, bundle_fields("boundaries"))
    by_name = {dp.field_name: dp.value for dp in read.answers}
    site.political_region = by_name.get("political_region")
    site.political_county = by_name.get("political_county")
    site.political_locality = by_name.get("political_locality")
    site.tract_geoid = by_name.get("tract_geoid")
    session.add(site)
    session.commit()
    session.refresh(site)


@router.post("/sites", tags=["sites"])
async def create_site(
    body: SiteCreate,
    session: Session = Depends(get_session),
    client: MireyeClient = Depends(get_client),
) -> dict[str, Any]:
    """Register a monitored site. Geocodes once, ever, when given an address; accepts a
    direct lat/lng otherwise (Build Brief II permits monitoring "a county, a town, or a
    single address" -- direct coordinates are a first-class path, not just a fallback).
    """
    if body.address and (body.lat is not None or body.lng is not None):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_locator", "message": "send address or lat+lng, never both"},
        )

    if body.address:
        try:
            geo = await client.geocode(body.address)
        except MireyeError as exc:
            raise _http_error(exc) from exc
        site = Site(
            label=body.label,
            address_raw=body.address,
            lat=geo.lat,
            lng=geo.lng,
            geocode_accuracy=str(geo.accuracy) if geo.accuracy is not None else None,
            accuracy_type=geo.accuracy_type,
            match_type=geo.match_type,
            normalized_address=geo.normalized_address,
            geocode_provider=geo.provider,
            parcel_grade=geo.parcel_grade,
            precision_note=geo.precision_note,
            geocoded_at=utcnow(),
        )
    else:
        if body.lat is None or body.lng is None:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_locator", "message": "need address or lat+lng"},
            )
        site = Site(label=body.label, lat=body.lat, lng=body.lng)

    session.add(site)
    session.commit()
    session.refresh(site)

    await _apply_boundaries(session, client, site)

    return _site_json(site)


@router.get("/sites/{site_id}", tags=["sites"])
def get_site(site_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    site = session.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail={"error": "site_not_found", "message": site_id})
    return _site_json(site)


@router.get("/sites/{site_id}/{bundle_name}", tags=["sites"])
async def get_bundle(
    site_id: str,
    bundle_name: str,
    session: Session = Depends(get_session),
    client: MireyeClient = Depends(get_client),
) -> dict[str, Any]:
    """The primary read path: cached fields are served as-is, anything missing/stale is
    fetched in one call, quoted first, logged after -- Phase 3 never has to make a
    second call, and the cost of every read stays visible here and in /v1/fetch-log.
    """
    site = session.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail={"error": "site_not_found", "message": site_id})
    try:
        field_names = bundle_fields(bundle_name)
    except UnknownBundle as exc:
        raise HTTPException(status_code=400, detail={"error": "unknown_bundle", "message": str(exc)})

    settings = get_settings()
    read = read_fields(session, site_id, field_names)
    hits = len(read.answers) + len(read.withheld)
    to_fetch = read.to_fetch
    credits_spent = 0
    if to_fetch and settings.phase2_autofetch_on_miss:
        try:
            await fetch_and_store(session, client, site, to_fetch, trigger="cache_miss", caller_ref=None)
        except MireyeError as exc:
            raise _http_error(exc) from exc
        credits_spent = estimate_credits(to_fetch)
        read = read_fields(session, site_id, field_names)  # re-read post-fetch

    rows = read.answers + read.withheld
    return {
        "bundle": bundle_name,
        "site_id": site_id,
        "cache": {"hits": hits, "fetched": len(to_fetch) if credits_spent else 0, "credits_spent": credits_spent},
        "datapoints": [serialize(dp) for dp in rows],
    }


# --------------------------------------------------------------------------
# fetch log -- the demo evidence that N events cost N fetches, not N x documents
# --------------------------------------------------------------------------
@router.get("/fetch-log", tags=["ops"])
def fetch_log(
    site_id: Optional[str] = None,
    limit: int = Query(100, le=1000),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(FetchLog).order_by(FetchLog.created_at.desc()).limit(limit)
    if site_id:
        query = query.where(FetchLog.site_id == site_id)
    rows = session.exec(query).all()
    return [row.model_dump(mode="json") for row in rows]


# --------------------------------------------------------------------------
# console read surface -- cache-only, costs nothing
# --------------------------------------------------------------------------
@router.get("/sites", tags=["sites"])
def list_sites(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Every registered site, newest first, straight out of `p2_site`.

    Deliberately cache-only, and that is the point rather than an omission: it touches
    only the Site rows, so it can never geocode, never pull a bundle, and never cost a
    credit. A console that draws a site list on every page load must be free to call.

    It reuses `_site_json` instead of shaping its own payload so that a site read one at
    a time (`GET /v1/sites/{site_id}`) and the same site read in a list can never drift
    apart -- including `degraded`, which is a property rather than a column and is the
    one field a console must not silently lose.

    No `event_type` filter (or any other event-shaped parameter) belongs on this route:
    Phase 2 is event-blind by design, and every event-side question is Phase 3's.
    """
    rows = session.exec(select(Site).order_by(Site.created_at.desc())).all()
    return [_site_json(site) for site in rows]
