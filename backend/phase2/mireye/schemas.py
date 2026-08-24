"""Wire schemas for the Mireye Earth API.

These mirror the documented response shapes. The important one is `FieldRecord.status`,
which is tri-state and is the field to read -- presence in `fields` is not success.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

FieldStatus = Literal["ok", "absent", "failed"]

#: Statuses that represent a real answer from the source and may be cached to ttl_seconds.
CACHEABLE_STATUSES: frozenset[str] = frozenset({"ok", "absent"})


class FieldRecord(BaseModel):
    """Mireye's per-field response record.

    - ``ok``     -- a real value.
    - ``absent`` -- valid no-data. The source answered "nothing here". Bills normally,
      and is EVIDENCE, not a missing field.
    - ``failed`` -- the fetch errored. ``value`` is null, ``error``/``retryable`` inline,
      refunded automatically. HTTP is still 200, so this must never be cached.
    """

    value: Any = None
    unit: Optional[str] = None
    status: FieldStatus
    error: Optional[str] = None
    retryable: Optional[bool] = None

    source: Optional[str] = None
    source_url: Optional[str] = None
    confidence: Optional[str] = None
    fetched_at: Optional[datetime] = None
    dataset_vintage: Optional[str] = None
    ttl_seconds: Optional[int] = None
    notes: Optional[str] = None

    @property
    def cacheable(self) -> bool:
        return self.status in CACHEABLE_STATUSES


class ResolvedLocation(BaseModel):
    """On EVERY response. A wrong-place answer is only catchable if the place is stated."""

    lat: float
    lng: float
    source: Optional[str] = None


class GeocodeBlock(BaseModel):
    accuracy: Optional[float] = None
    accuracy_type: Optional[str] = None
    match_type: Optional[str] = None
    normalized_address: Optional[str] = None
    provider: Optional[str] = None
    source: Optional[str] = None
    parcel_grade: Optional[bool] = None
    precision_note: Optional[str] = None


class FetchResponse(BaseModel):
    fields: dict[str, FieldRecord] = Field(default_factory=dict)
    resolved_location: Optional[ResolvedLocation] = None
    geocode: Optional[GeocodeBlock] = None
    partial_failures: list[Any] = Field(default_factory=list)
    notes: Optional[Any] = None
    data_gaps: Optional[Any] = None


class QuoteAllowance(BaseModel):
    credits_included: Optional[int] = None
    credits_used: Optional[int] = None
    credits_remaining: Optional[int] = None
    self_imposed_limit: Optional[int] = None
    effective_limit: Optional[int] = None
    limited_by: Optional[str] = None
    resets_at: Optional[datetime] = None
    would_exceed_allowance: Optional[bool] = None
    would_be_blocked: Optional[bool] = None


class QuoteResponse(BaseModel):
    credits_per_location: Optional[int] = None
    credits_total: Optional[int] = None
    breakdown: dict[str, Any] = Field(default_factory=dict)
    allowance: Optional[QuoteAllowance] = None
    notes: Optional[Any] = None


class GeocodeResponse(BaseModel):
    lat: float
    lng: float
    accuracy: Optional[float] = None
    accuracy_type: Optional[str] = None
    match_type: Optional[str] = None
    normalized_address: Optional[str] = None
    provider: Optional[str] = None
    source: Optional[str] = None
    parcel_grade: Optional[bool] = None
    precision_note: Optional[str] = None


class MireyeError(Exception):
    """One error shape: {"detail": {"error", "message", "retryable", ...}}.

    Honour ``retryable``, not the HTTP status code.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
        retry_after: Optional[str] = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after = retry_after
