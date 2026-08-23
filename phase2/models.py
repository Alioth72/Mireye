"""SQLModel tables for Phase 2.

Every table name is prefixed ``p2_`` so that a post-merge shared database with Phase 3
has no collisions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Site(SQLModel, table=True):
    """A monitored location. Geocoded exactly once, ever."""

    __tablename__ = "p2_site"

    id: str = Field(default_factory=_uuid, primary_key=True)
    label: Optional[str] = None
    address_raw: Optional[str] = None

    lat: float
    lng: float

    # Geocode provenance. `accuracy_type` matters more than `accuracy`, and
    # `parcel_grade=False` means the coordinate may be a neighbour's parcel.
    geocode_accuracy: Optional[str] = None
    accuracy_type: Optional[str] = None
    match_type: Optional[str] = None
    normalized_address: Optional[str] = None
    geocode_provider: Optional[str] = None
    parcel_grade: Optional[bool] = None
    precision_note: Optional[str] = None
    geocoded_at: Optional[datetime] = None

    # Boundaries bundle, pulled once at registration. Phase 3 uses these for its own
    # scope resolution; we store and serve them, we do not interpret them.
    political_region: Optional[str] = None
    political_county: Optional[str] = None
    political_locality: Optional[str] = None
    tract_geoid: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)

    @property
    def degraded(self) -> bool:
        """True when physical answers for this site may describe the wrong parcel."""
        return self.parcel_grade is False


class Datapoint(SQLModel, table=True):
    """One row per (site, field). Mirrors Mireye's per-field record exactly.

    Nothing is flattened away: `status` is tri-state and `absent` is a real answer,
    not a missing value.
    """

    __tablename__ = "p2_datapoint"
    __table_args__ = (UniqueConstraint("site_id", "field_name", name="uq_p2_datapoint_site_field"),)

    id: str = Field(default_factory=_uuid, primary_key=True)
    site_id: str = Field(index=True, foreign_key="p2_site.id")
    field_name: str = Field(index=True)

    value: Optional[Any] = Field(default=None, sa_column=Column(JSON))
    unit: Optional[str] = None

    # 'ok' | 'absent' | 'failed'
    status: str
    error: Optional[str] = None
    retryable: Optional[bool] = None

    source: Optional[str] = None
    source_url: Optional[str] = None
    license: Optional[str] = None
    confidence: Optional[str] = None

    fetched_at: Optional[datetime] = None  # authoritative as-of, NOT created_at
    dataset_vintage: Optional[str] = None
    ttl_seconds: Optional[int] = None
    notes: Optional[str] = None

    request_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class FetchLog(SQLModel, table=True):
    """Credit audit trail. This is demo evidence, not ops hygiene: it is how the team
    proves N events cost N fetches and not N x documents."""

    __tablename__ = "p2_fetch_log"

    id: str = Field(default_factory=_uuid, primary_key=True)
    site_id: Optional[str] = Field(default=None, index=True)

    fields: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    quoted_credits: Optional[int] = None
    charged_credits: Optional[int] = None
    credits_remaining: Optional[int] = None

    request_id: Optional[str] = None
    idempotency_key: Optional[str] = None

    # 'registration' | 'cache_miss' | 'refresh' | 'replay'
    trigger: str = "cache_miss"

    # Opaque. Phase 3 may pass a canonical event id here for its own audit;
    # Phase 2 never parses it.
    caller_ref: Optional[str] = None

    ok: bool = True
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow, index=True)


class ScoreProfile(SQLModel, table=True):
    """Named weight/threshold set for a derived metric.

    Ships with a `default` row per metric. Phase 3 may store replay-tuned profiles.
    A profile can change weights and thresholds; it cannot change the multiplicative
    composition rule.
    """

    __tablename__ = "p2_score_profile"
    __table_args__ = (UniqueConstraint("metric", "name", name="uq_p2_score_profile"),)

    id: str = Field(default_factory=_uuid, primary_key=True)
    metric: str = Field(index=True)
    name: str = Field(default="default", index=True)

    weights: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    thresholds: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=utcnow)


class CatalogCache(SQLModel, table=True):
    """Local mirror of GET /v1/meta/fields so field names are validated before sending."""

    __tablename__ = "p2_catalog_cache"

    id: str = Field(default="fields", primary_key=True)
    etag: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    fetched_at: datetime = Field(default_factory=utcnow)
