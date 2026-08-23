"""Phase 2 HTTP surface.

Everything hangs off a single ``APIRouter`` with no module-level app object, so the
post-merge integration into Phase 3 is ``app.include_router(phase2.router)`` and
nothing else changes.

Route ordering note: the specific ``/datapoints`` and ``/derived`` paths are declared
before the catch-all ``/{bundle}`` path, because FastAPI matches in declaration order.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlmodel import Session, select

from . import catalog, scoring
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
from .models import Datapoint, FetchLog, ScoreProfile, Site, utcnow
from .mireye.client import MireyeClient
from .mireye.schemas import MireyeError
from .orchestrator import read_or_fetch
from .store import read_fields, serialize

router = APIRouter(prefix="/v1", tags=["phase2"])


# --------------------------------------------------------------------------
# dependencies & helpers
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


def _get_site(session: Session, site_id: str) -> Site:
    site = session.get(Site, site_id)
    if site is None:
        raise HTTPException(
            status_code=404, detail={"error": "site_not_found", "message": site_id}
        )
    return site


def _site_payload(site: Site) -> dict[str, Any]:
    payload = {
        "id": site.id,
        "label": site.label,
        "address_raw": site.address_raw,
        "lat": site.lat,
        "lng": site.lng,
        "geocode": {
            "accuracy": site.geocode_accuracy,
            "accuracy_type": site.accuracy_type,
            "match_type": site.match_type,
            "normalized_address": site.normalized_address,
            "provider": site.geocode_provider,
            "parcel_grade": site.parcel_grade,
            "precision_note": site.precision_note,
            "geocoded_at": site.geocoded_at,
        },
        "boundaries": {
            "political_region": site.political_region,
            "political_county": site.political_county,
            "political_locality": site.political_locality,
            "tract_geoid": site.tract_geoid,
        },
        "degraded": site.degraded,
        "created_at": site.created_at,
    }
    if site.degraded:
        payload["warning"] = (
            "parcel_grade is false -- this coordinate may sit on a neighbouring parcel. "
            "Physical answers here describe that coordinate, which may not be the "
            "intended property. Route to review."
        )
    return payload


def _outcome_payload(outcome) -> dict[str, Any]:
    return {
        "fetched": outcome.fetched,
        "credits_quoted": outcome.quoted_credits,
        "credits_spent": outcome.charged_credits,
        "credits_remaining": outcome.credits_remaining,
        "request_id": outcome.request_id,
        "resolved_location_ok": outcome.resolved_location_ok,
        "warnings": outcome.warnings,
        "error": outcome.error,
    }


# --------------------------------------------------------------------------
# health, catalog, bundles
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
# quote, budget, fetch log
# --------------------------------------------------------------------------
class QuoteRequest(BaseModel):
    bundles: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    locations: int = 1


@router.post("/quote", tags=["ops"])
async def quote(
    body: QuoteRequest, client: MireyeClient = Depends(get_client)
) -> dict[str, Any]:
    """Free, unmetered, exact -- computed by the same code that charges."""
    try:
        selection = fields_for(body.bundles) if body.bundles else []
    except UnknownBundle as exc:
        raise HTTPException(400, {"error": "unknown_bundle", "message": str(exc)})

    for field in body.fields:
        if field not in selection:
            selection.append(field)
    if not selection:
        raise HTTPException(
            400, {"error": "no_fields_requested", "message": "send bundles and/or fields"}
        )
    if len(selection) > MAX_FIELDS_PER_FETCH:
        raise HTTPException(
            400,
            {
                "error": "fields_too_many",
                "message": f"{len(selection)} exceeds the {MAX_FIELDS_PER_FETCH} cap",
            },
        )

    try:
        remote = await client.quote(fields=selection, locations=body.locations)
    except MireyeError as exc:
        raise _http_error(exc) from exc

    return {
        "fields": selection,
        "locations": body.locations,
        "local_estimate_credits": estimate_credits(selection) * body.locations,
        "touches_parcel_record": sorted(touches_parcel_record(selection)),
        "quote": remote.model_dump(mode="json"),
    }


@router.get("/budget", tags=["ops"])
async def budget(client: MireyeClient = Depends(get_client)) -> dict[str, Any]:
    """Informational. There is no budget gate -- see context/phase2.md D5."""
    try:
        return await client.usage()
    except MireyeError as exc:
        raise _http_error(exc) from exc


@router.get("/fetch-log", tags=["ops"])
def fetch_log(
    limit: int = Query(50, le=500),
    site_id: Optional[str] = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """The credit audit trail. This is demo evidence: it shows that N events cost N
    fetches and not N x documents."""
    stmt = select(FetchLog).order_by(FetchLog.created_at.desc()).limit(limit)
    if site_id:
        stmt = (
            select(FetchLog)
            .where(FetchLog.site_id == site_id)
            .order_by(FetchLog.created_at.desc())
            .limit(limit)
        )
    rows = session.exec(stmt).all()
    return {
        "total_credits_spent": sum(r.charged_credits or 0 for r in rows),
        "fetch_count": len(rows),
        "entries": [
            {
                "created_at": r.created_at,
                "site_id": r.site_id,
                "fields": r.fields,
                "quoted": r.quoted_credits,
                "charged": r.charged_credits,
                "trigger": r.trigger,
                "caller_ref": r.caller_ref,
                "ok": r.ok,
                "error": r.error,
                "request_id": r.request_id,
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------
# sites
# --------------------------------------------------------------------------
class SiteCreate(BaseModel):
    label: Optional[str] = None
    address: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    skip_boundaries: bool = False

    @model_validator(mode="after")
    def _one_locator(self):
        has_coord = self.lat is not None and self.lng is not None
        if has_coord and self.address:
            raise ValueError("send address or lat+lng, never both")
        if not has_coord and not self.address:
            raise ValueError("send address or lat+lng")
        return self


@router.post("/sites", tags=["sites"])
async def create_site(
    body: SiteCreate,
    session: Session = Depends(get_session),
    client: MireyeClient = Depends(get_client),
) -> dict[str, Any]:
    """Register a monitored site. The ONLY place geocoding happens -- 1 credit, once ever.

    Also pulls the `boundaries` bundle (4 credits) because Phase 3 needs
    political_locality/county for scope resolution and those values never change.
    """
    site = Site(lat=body.lat or 0.0, lng=body.lng or 0.0, label=body.label)

    if body.address:
        try:
            geo = await client.geocode(body.address)
        except MireyeError as exc:
            raise _http_error(exc) from exc
        site.address_raw = body.address
        site.lat, site.lng = geo.lat, geo.lng
        site.geocode_accuracy = geo.accuracy
        site.accuracy_type = geo.accuracy_type
        site.match_type = geo.match_type
        site.normalized_address = geo.normalized_address
        site.geocode_provider = geo.provider
        site.parcel_grade = geo.parcel_grade
        site.precision_note = geo.precision_note
        site.geocoded_at = utcnow()

    session.add(site)
    session.commit()
    session.refresh(site)

    boundaries: dict[str, Any] = {}
    if not body.skip_boundaries:
        answers, outcome = await read_or_fetch(
            session, client, site, list(bundle_fields("boundaries")), trigger="registration"
        )
        for dp in answers:
            if hasattr(site, dp.field_name):
                setattr(site, dp.field_name, dp.value)
        session.add(site)
        session.commit()
        session.refresh(site)
        boundaries = _outcome_payload(outcome)

    payload = _site_payload(site)
    payload["registration_fetch"] = boundaries
    return payload


@router.get("/sites", tags=["sites"])
def list_sites(session: Session = Depends(get_session)) -> dict[str, Any]:
    sites = session.exec(select(Site)).all()
    return {"count": len(sites), "sites": [_site_payload(s) for s in sites]}


@router.get("/sites/{site_id}", tags=["sites"])
def get_site(site_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    return _site_payload(_get_site(session, site_id))


@router.delete("/sites/{site_id}", tags=["sites"])
def delete_site(site_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    site = _get_site(session, site_id)
    for dp in session.exec(select(Datapoint).where(Datapoint.site_id == site_id)).all():
        session.delete(dp)
    session.delete(site)
    session.commit()
    return {"deleted": site_id}


# --------------------------------------------------------------------------
# datapoints -- declared BEFORE /{bundle} so the catch-all does not shadow them
# --------------------------------------------------------------------------
@router.get("/sites/{site_id}/datapoints", tags=["datapoints"])
def all_datapoints(site_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Everything held for this site. Cache-only -- never spends."""
    _get_site(session, site_id)
    rows = session.exec(select(Datapoint).where(Datapoint.site_id == site_id)).all()
    now = utcnow()
    return {
        "site_id": site_id,
        "count": len(rows),
        "datapoints": [serialize(dp, now=now) for dp in rows],
    }


@router.get("/sites/{site_id}/datapoints/{field_name}", tags=["datapoints"])
def one_datapoint(
    site_id: str, field_name: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    _get_site(session, site_id)
    dp = session.exec(
        select(Datapoint).where(
            Datapoint.site_id == site_id, Datapoint.field_name == field_name
        )
    ).first()
    if dp is None:
        raise HTTPException(
            404,
            {
                "error": "datapoint_not_cached",
                "message": f"{field_name} not held for this site; fetch a bundle first",
            },
        )
    return serialize(dp)


class RefreshRequest(BaseModel):
    bundles: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    caller_ref: Optional[str] = None


@router.post("/sites/{site_id}/datapoints:refresh", tags=["datapoints"])
async def refresh_datapoints(
    site_id: str,
    body: RefreshRequest,
    session: Session = Depends(get_session),
    client: MireyeClient = Depends(get_client),
) -> dict[str, Any]:
    """Force a refetch even when the cached value is still fresh."""
    from .orchestrator import fetch_fields

    site = _get_site(session, site_id)
    try:
        selection = fields_for(body.bundles) if body.bundles else []
    except UnknownBundle as exc:
        raise HTTPException(400, {"error": "unknown_bundle", "message": str(exc)})
    for field in body.fields:
        if field not in selection:
            selection.append(field)
    if not selection:
        raise HTTPException(
            400, {"error": "no_fields_requested", "message": "send bundles and/or fields"}
        )

    outcome = await fetch_fields(
        session, client, site, selection, trigger="refresh", caller_ref=body.caller_ref
    )
    read = read_fields(session, site_id, selection)
    now = utcnow()
    return {
        "site_id": site_id,
        "fetch": _outcome_payload(outcome),
        "datapoints": [serialize(dp, now=now) for dp in read.answers],
    }


# --------------------------------------------------------------------------
# derived scores
# --------------------------------------------------------------------------
class ScoreOverride(BaseModel):
    weights: Optional[dict[str, float]] = None
    thresholds: Optional[dict[str, Any]] = None
    caller_ref: Optional[str] = None


async def _derived(
    session: Session,
    client: MireyeClient,
    site_id: str,
    metric: str,
    *,
    weights=None,
    thresholds=None,
    profile_name: str = "default",
    caller_ref: Optional[str] = None,
) -> dict[str, Any]:
    if metric not in scoring.METRICS:
        raise HTTPException(
            404,
            {
                "error": "unknown_metric",
                "message": f"{metric}; known: {', '.join(scoring.METRICS)}",
            },
        )
    site = _get_site(session, site_id)
    answers, outcome = await read_or_fetch(
        session,
        client,
        site,
        scoring.required_fields(metric),
        trigger="cache_miss",
        caller_ref=caller_ref,
    )
    result = scoring.score(
        metric, answers, weights=weights, thresholds=thresholds, profile_name=profile_name
    )
    result["site_id"] = site_id
    result["fetch"] = _outcome_payload(outcome)
    if site.degraded:
        result["confidence"] = "low"
        result["warning"] = "parcel_grade is false -- coordinate may be a neighbour's parcel"
    return result


@router.get("/sites/{site_id}/derived/{metric}", tags=["derived"])
async def derived_score(
    site_id: str,
    metric: str,
    profile: str = Query("default"),
    session: Session = Depends(get_session),
    client: MireyeClient = Depends(get_client),
) -> dict[str, Any]:
    """A capability measure -- 'could this ground host X'. Never materiality."""
    weights = thresholds = None
    if profile != "default":
        row = session.exec(
            select(ScoreProfile).where(
                ScoreProfile.metric == metric, ScoreProfile.name == profile
            )
        ).first()
        if row is None:
            raise HTTPException(
                404, {"error": "unknown_profile", "message": f"{metric}/{profile}"}
            )
        weights, thresholds = row.weights, row.thresholds
    return await _derived(
        session, client, site_id, metric,
        weights=weights, thresholds=thresholds, profile_name=profile,
    )


@router.post("/sites/{site_id}/derived/{metric}", tags=["derived"])
async def derived_score_override(
    site_id: str,
    metric: str,
    body: ScoreOverride,
    session: Session = Depends(get_session),
    client: MireyeClient = Depends(get_client),
) -> dict[str, Any]:
    """Inline weight/threshold override. Composition stays multiplicative (D11)."""
    return await _derived(
        session, client, site_id, metric,
        weights=body.weights, thresholds=body.thresholds,
        profile_name="inline_override", caller_ref=body.caller_ref,
    )


class ProfileCreate(BaseModel):
    metric: str
    name: str
    weights: dict[str, float] = Field(default_factory=dict)
    thresholds: dict[str, Any] = Field(default_factory=dict)


@router.post("/score-profiles", tags=["derived"])
def create_profile(
    body: ProfileCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    if body.metric not in scoring.METRICS:
        raise HTTPException(400, {"error": "unknown_metric", "message": body.metric})
    row = session.exec(
        select(ScoreProfile).where(
            ScoreProfile.metric == body.metric, ScoreProfile.name == body.name
        )
    ).first()
    if row is None:
        row = ScoreProfile(metric=body.metric, name=body.name)
        session.add(row)
    row.weights, row.thresholds = body.weights, body.thresholds
    session.commit()
    session.refresh(row)
    return {"metric": row.metric, "name": row.name, "weights": row.weights,
            "thresholds": row.thresholds}


@router.get("/score-profiles", tags=["derived"])
def list_profiles(session: Session = Depends(get_session)) -> dict[str, Any]:
    rows = session.exec(select(ScoreProfile)).all()
    return {
        "defaults": {
            m: {"weights": w, "composition": "weighted_geometric_mean"}
            for m, w in scoring.DEFAULT_WEIGHTS.items()
        },
        "stored": [
            {"metric": r.metric, "name": r.name, "weights": r.weights,
             "thresholds": r.thresholds}
            for r in rows
        ],
    }


# --------------------------------------------------------------------------
# bundles -- catch-all, MUST stay last
# --------------------------------------------------------------------------
@router.get("/sites/{site_id}/{bundle}", tags=["bundles"])
async def get_bundle(
    site_id: str,
    bundle: str,
    caller_ref: Optional[str] = Query(
        None, description="Opaque; Phase 3 may pass a canonical event id for its own audit"
    ),
    session: Session = Depends(get_session),
    client: MireyeClient = Depends(get_client),
) -> dict[str, Any]:
    """The primary path. Serves cached fields and fetches whatever is missing or stale."""
    try:
        fields = bundle_fields(bundle)
    except UnknownBundle as exc:
        raise HTTPException(404, {"error": "unknown_bundle", "message": str(exc)})

    site = _get_site(session, site_id)
    before = read_fields(session, site_id, fields)
    answers, outcome = await read_or_fetch(
        session, client, site, list(fields), caller_ref=caller_ref
    )
    now = utcnow()
    return {
        "site_id": site_id,
        "bundle": bundle,
        "cache": {
            "hits": len(before.answers),
            "fetched": len(outcome.fetched),
            "credits_quoted": outcome.quoted_credits,
            "credits_spent": outcome.charged_credits,
        },
        "warnings": outcome.warnings,
        "datapoints": [serialize(dp, now=now) for dp in answers],
    }
