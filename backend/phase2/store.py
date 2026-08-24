"""The datapoint store -- the correctness core of Phase 2.

Two rules govern everything here.

**Never cache a `failed` field as an answer.** HTTP is 200 on a failed field, so a naive
upsert freezes a failure as truth. We do persist failed rows, but only as a *negative
cache* so a broken field is not re-fetched on every request; they are never returned as
answers and they expire on their own short window, not on `ttl_seconds`.

**`absent` is a real answer.** The source said "nothing here". It bills normally and it
is evidence -- `intersects_wetland: absent` raises optionality. It is never reported as
a missing field.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, NamedTuple, Optional

from sqlmodel import Session, select

from .config import Settings, get_settings
from .models import Datapoint, utcnow
from .mireye.licenses import license_for
from .mireye.schemas import CACHEABLE_STATUSES, FieldRecord


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips datetimes without tzinfo; treat naive values as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_answer(dp: Datapoint) -> bool:
    """True when this row carries a real answer (`ok` or `absent`)."""
    return dp.status in CACHEABLE_STATUSES


def is_stale(dp: Datapoint, *, now: Optional[datetime] = None) -> bool:
    """Staleness against Mireye's own `ttl_seconds` hint.

    A row with no TTL is never considered stale on its own -- `fetched_at` is the
    authoritative as-of and callers can decide what to do with an old value.
    """
    if not is_answer(dp):
        return True
    fetched_at = _aware(dp.fetched_at)
    if fetched_at is None or dp.ttl_seconds is None:
        return False
    now = now or utcnow()
    return now - fetched_at > timedelta(seconds=dp.ttl_seconds)


def needs_fetch(
    dp: Optional[Datapoint],
    *,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> bool:
    """Should this field go on the wire?

    Missing -> yes. Fresh answer -> no. Stale answer -> yes.
    Failed -> yes, but not before the negative-cache window expires.
    """
    if dp is None:
        return True
    now = now or utcnow()
    if is_answer(dp):
        return is_stale(dp, now=now)

    settings = settings or get_settings()
    failed_at = _aware(dp.updated_at) or now
    window = timedelta(seconds=settings.phase2_failed_negative_cache_s)
    if dp.retryable is False:
        # A structured upstream refusal -- retrying will not help. Back off much harder.
        window = window * 12
    return now - failed_at > window


class BundleRead(NamedTuple):
    answers: list[Datapoint]
    """Rows carrying a real, fresh answer (`ok` or `absent`)."""

    to_fetch: list[str]
    """Field names that must go on the wire: missing, stale, or a lapsed failure."""

    withheld: list[Datapoint]
    """Failed rows inside their negative-cache window. Reported, never served as values."""


def read_fields(
    session: Session,
    site_id: str,
    fields: Iterable[str],
    *,
    now: Optional[datetime] = None,
    settings: Optional[Settings] = None,
) -> BundleRead:
    """Split a field selection into what we can serve and what we must fetch."""
    wanted = list(dict.fromkeys(fields))
    now = now or utcnow()
    settings = settings or get_settings()

    rows = session.exec(
        select(Datapoint).where(Datapoint.site_id == site_id, Datapoint.field_name.in_(wanted))
    ).all()
    by_name = {row.field_name: row for row in rows}

    answers: list[Datapoint] = []
    to_fetch: list[str] = []
    withheld: list[Datapoint] = []

    for name in wanted:
        dp = by_name.get(name)
        if needs_fetch(dp, now=now, settings=settings):
            to_fetch.append(name)
            continue
        if dp is None:  # pragma: no cover -- needs_fetch(None) is always True
            to_fetch.append(name)
        elif is_answer(dp):
            answers.append(dp)
        else:
            withheld.append(dp)

    return BundleRead(answers=answers, to_fetch=to_fetch, withheld=withheld)


def upsert_records(
    session: Session,
    site_id: str,
    records: dict[str, FieldRecord],
    *,
    request_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> list[Datapoint]:
    """Persist a fetch result.

    `ok` and `absent` are stored as answers with their provenance and resolved licence.
    `failed` is stored as a negative-cache marker only: value stays null, and the row is
    never served as an answer.
    """
    now = now or utcnow()
    stored: list[Datapoint] = []

    for name, record in records.items():
        dp = session.exec(
            select(Datapoint).where(
                Datapoint.site_id == site_id, Datapoint.field_name == name
            )
        ).first()

        if dp is None:
            dp = Datapoint(site_id=site_id, field_name=name, status=record.status)
            session.add(dp)

        if record.cacheable:
            dp.status = record.status
            dp.value = record.value
            dp.unit = record.unit
            dp.error = None
            dp.retryable = None
            dp.source = record.source
            dp.source_url = record.source_url
            dp.license = license_for(record.source)
            dp.confidence = record.confidence or "unknown"
            dp.fetched_at = _aware(record.fetched_at) or now
            dp.dataset_vintage = record.dataset_vintage
            dp.ttl_seconds = record.ttl_seconds
            dp.notes = record.notes
        else:
            # Negative cache only. We deliberately do NOT overwrite a previously good
            # value with a failure, and we never let a failure present as an answer.
            if is_answer(dp) and dp.value is not None:
                dp.notes = (
                    f"refresh failed ({record.error or 'unknown error'}); "
                    "serving last known value"
                )
                dp.updated_at = now
                stored.append(dp)
                continue
            dp.status = "failed"
            dp.value = None
            dp.error = record.error
            dp.retryable = record.retryable
            dp.notes = record.notes
            dp.confidence = "unknown"

        dp.request_id = request_id
        dp.updated_at = now
        stored.append(dp)

    session.commit()
    for dp in stored:
        session.refresh(dp)
    return stored


def serialize(dp: Datapoint, *, now: Optional[datetime] = None) -> dict:
    """Public representation of a datapoint. Provenance is never stripped."""
    now = now or utcnow()
    payload = {
        "field": dp.field_name,
        "value": dp.value,
        "unit": dp.unit,
        "status": dp.status,
        "source": dp.source,
        "source_url": dp.source_url,
        "license": dp.license,
        "confidence": dp.confidence,
        "fetched_at": _aware(dp.fetched_at),
        "dataset_vintage": dp.dataset_vintage,
        "ttl_seconds": dp.ttl_seconds,
        "stale": is_stale(dp, now=now),
        "notes": dp.notes,
    }
    if dp.status == "failed":
        payload["error"] = dp.error
        payload["retryable"] = dp.retryable
    return payload
