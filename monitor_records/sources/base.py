from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawRecord:
    """Source-agnostic representation of one fetched government artifact,
    before normalization into the Document model."""

    source: str
    external_id: str
    document_type: str
    title: str | None
    source_url: str | None
    published_at: datetime | None
    meeting_date: datetime | None
    raw_text: str | None
    metadata: dict = field(default_factory=dict)


class RecordSource(ABC):
    """Interface every jurisdiction/system adapter implements.

    Keep this interface stable -- it's what lets us add Granicus/CivicClerk/
    PrimeGov later without touching classify/extract/canonicalize.
    """

    name: str

    @abstractmethod
    async def discover(self, since: datetime | None = None) -> list[str]:
        """Return external_ids of records that are new/updated since `since`.
        Should be cheap -- metadata-only listing, not full document fetch.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch(self, external_id: str) -> list[RawRecord]:
        """Fetch full record(s) for one external_id.

        Returns a list because one external_id (e.g. one Matter) can expand
        into several RawRecords (the matter itself, its history, attachments).
        """
        raise NotImplementedError
