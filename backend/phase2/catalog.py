"""Local mirror of Mireye's field catalog.

`GET /v1/meta/fields` is public and ETag-cached. Mirroring it lets us validate field
names locally before sending, so `400 fields_unknown` never reaches production and a
typo in a bundle definition fails at startup rather than mid-demo.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable, Optional

from sqlmodel import Session, select

from .config import get_settings
from .models import CatalogCache, utcnow
from .mireye.client import MireyeClient
from .store import _aware


def _extract_field_names(payload: dict[str, Any]) -> set[str]:
    """The catalog payload nests field names under a few plausible shapes; accept any."""
    fields = payload.get("fields")
    names: set[str] = set()
    if isinstance(fields, dict):
        names.update(fields.keys())
    elif isinstance(fields, list):
        for item in fields:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("field")
                if name:
                    names.add(name)
    return names


def cached_payload(session: Session) -> Optional[CatalogCache]:
    return session.exec(select(CatalogCache).where(CatalogCache.id == "fields")).first()


def is_fresh(row: Optional[CatalogCache]) -> bool:
    if row is None:
        return False
    fetched_at = _aware(row.fetched_at)
    if fetched_at is None:
        return False
    ttl = get_settings().phase2_catalog_ttl_s
    return utcnow() - fetched_at <= timedelta(seconds=ttl)


async def refresh(session: Session, client: MireyeClient, *, force: bool = False) -> CatalogCache:
    row = cached_payload(session)
    if row is not None and is_fresh(row) and not force:
        return row

    payload, etag = await client.meta_fields(etag=row.etag if row else None)

    if row is None:
        row = CatalogCache(id="fields")
        session.add(row)
    if payload is not None:  # None means 304 Not Modified -- keep what we have
        row.payload = payload
        row.etag = etag
    row.fetched_at = utcnow()
    session.commit()
    session.refresh(row)
    return row


def known_field_names(session: Session) -> set[str]:
    row = cached_payload(session)
    return _extract_field_names(row.payload) if row else set()


def unknown_fields(session: Session, fields: Iterable[str]) -> set[str]:
    """Fields we do not recognise. Empty set when the catalog has not been mirrored yet
    -- we do not block on a cache we never populated."""
    known = known_field_names(session)
    if not known:
        return set()
    return {f for f in fields if f not in known}
