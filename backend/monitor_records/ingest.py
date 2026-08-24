"""
The ingestion pipeline. This is the piece that ties everything else in this
package together:

    RecordSource.fetch(external_id)
        -> persist raw Documents
        -> classify.is_potentially_relevant() (cheap filter)
        -> stage_resolver (deterministic, from Legistar history/status)
           + extract.call_llm_extract (LLM, for event_type/title/geography;
             optional -- falls back to classify.guess_event_type() heuristic
             if no ANTHROPIC_API_KEY is set or the call fails)
        -> canonicalize.upsert_event() (dedup + stage transitions)
        -> Evidence rows

Deterministic stage always wins over the LLM's opinion when both are
available -- see resolve_stage() below. This is the load-bearing rule from
the spec: legal/status state must come from reliable structured evidence,
not LLM inference.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from . import classify, geo
from .canonicalize import canonical_event_id, upsert_event
from .extract import ExtractedEvent, call_llm_extract
from .models import Document, EventStage
from .sources.base import RawRecord, RecordSource
from .stage_resolver import latest_stage_from_history, resolve_from_matter_status

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    matter_external_id: str
    skipped_reason: str | None = None
    event_id: str | None = None
    canonical_id: str | None = None
    created: bool = False
    stage_changed: bool = False
    stage: str | None = None
    used_llm: bool = False


def _content_hash(raw: RawRecord) -> str | None:
    basis = raw.raw_text or raw.title
    if not basis:
        return None
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _get_or_create_document(session: Session, raw: RawRecord) -> Document:
    existing = (
        session.query(Document)
        .filter_by(source=raw.source, external_id=raw.external_id)
        .one_or_none()
    )
    if existing is not None:
        # keep provenance current if the source record changed (e.g. status
        # field on the matter moved) -- cheap to just overwrite mutable fields
        existing.title = raw.title
        existing.raw_text = raw.raw_text
        existing.doc_metadata = raw.metadata
        existing.content_hash = _content_hash(raw)
        return existing

    doc = Document(
        source=raw.source,
        source_url=raw.source_url,
        external_id=raw.external_id,
        document_type=raw.document_type,
        title=raw.title,
        published_at=raw.published_at,
        meeting_date=raw.meeting_date,
        content_hash=_content_hash(raw),
        raw_text=raw.raw_text,
        doc_metadata=raw.metadata,
    )
    session.add(doc)
    session.flush()  # get doc.id
    return doc


def resolve_stage(
    matter_record: RawRecord, history_records: list[RawRecord], llm_stage: EventStage | None
) -> tuple[EventStage | None, datetime | None]:
    """Deterministic-first stage resolution.

    Checks, in order of trust:
      1. MatterHistory action rows (most granular, closest to an actual vote)
      2. The Matter's own MatterStatusName field
      3. The LLM's extracted stage (only if neither structured signal fired)
    """
    history_rows = [
        {
            "action_name": r.metadata.get("action_name"),
            "passed_flag": r.metadata.get("passed_flag"),
        }
        for r in history_records
    ]
    stage, row = latest_stage_from_history(history_rows)
    if stage is not None:
        occurred_at = None
        if row:
            matching = next(
                (
                    r
                    for r in history_records
                    if r.metadata.get("action_name") == row.get("action_name")
                    and r.metadata.get("passed_flag") == row.get("passed_flag")
                ),
                None,
            )
            occurred_at = matching.published_at if matching else None
        return stage, occurred_at

    status_stage = resolve_from_matter_status(matter_record.metadata.get("matter_status"))
    if status_stage is not None:
        occurred_at = matter_record.meeting_date or matter_record.published_at
        return status_stage, occurred_at

    if llm_stage is not None:
        return llm_stage, matter_record.published_at

    return None, None


async def ingest_matter(
    source: RecordSource,
    session: Session,
    external_id: str,
    *,
    use_llm: bool = True,
    jurisdiction: str = "Seattle",
) -> IngestResult:
    records = await source.fetch(external_id)

    matter_record = next((r for r in records if r.document_type == "matter"), None)
    if matter_record is None:
        return IngestResult(matter_external_id=external_id, skipped_reason="no matter record returned")

    history_records = [r for r in records if r.document_type == "history_action"]
    attachment_records = [r for r in records if r.document_type == "attachment"]

    for raw in records:
        _get_or_create_document(session, raw)
    session.flush()
    matter_doc = (
        session.query(Document)
        .filter_by(source=matter_record.source, external_id=matter_record.external_id)
        .one()
    )

    if not classify.is_potentially_relevant(matter_record.title, matter_record.raw_text, "matter"):
        return IngestResult(matter_external_id=external_id, skipped_reason="not relevant (keyword filter)")

    extraction: ExtractedEvent | None = None
    used_llm = False
    if use_llm:
        try:
            extraction = call_llm_extract(
                document_type="matter",
                title=matter_record.title,
                date=matter_record.metadata.get("intro_date"),
                source_url=matter_record.source_url,
                raw_text=matter_record.raw_text or matter_record.title or "",
            )
            used_llm = True
        except Exception as e:  # noqa: BLE001 -- deliberately broad: one bad
            # extraction must not abort the whole ingest run
            logger.warning("LLM extraction failed for %s, falling back to heuristic: %s", external_id, e)

    if extraction is not None and not extraction.is_material_event:
        return IngestResult(matter_external_id=external_id, skipped_reason="LLM judged not material", used_llm=used_llm)

    llm_stage = extraction.stage if extraction else None
    stage, stage_occurred_at = resolve_stage(matter_record, history_records, llm_stage)
    if stage is None:
        return IngestResult(
            matter_external_id=external_id,
            skipped_reason="could not determine stage from any signal",
            used_llm=used_llm,
        )

    if extraction is not None and extraction.event_type is not None:
        event_type = extraction.event_type
        title = extraction.title or matter_record.title
        description = extraction.description
        subject = extraction.subject
        confidence = extraction.confidence
        geography_extracted = extraction.geographic_scope.model_dump() if extraction.geographic_scope else None
        evidence_items = extraction.evidence
    else:
        # heuristic fallback path -- no LLM available/failed
        guessed = classify.guess_event_type(matter_record.title, matter_record.raw_text)
        if guessed is None:
            return IngestResult(
                matter_external_id=external_id,
                skipped_reason="passed keyword filter but heuristic couldn't assign an event_type "
                "(needs LLM extraction to classify)",
                used_llm=used_llm,
            )
        event_type = guessed
        title = matter_record.title or external_id
        description = None
        subject = None  # heuristic path has no way to distinguish e.g. data center vs BESS
        confidence = 0.4  # heuristic-only, deliberately low
        geography_extracted = None
        evidence_items = []

    geography_type, geography = geo.resolve_geography(geography_extracted)

    cid = canonical_event_id(
        jurisdiction,
        source.name,
        matter_record.metadata.get("matter_file"),
        fallback_title=title,
        fallback_date_bucket=(matter_record.published_at.strftime("%Y-%m") if matter_record.published_at else None),
    )

    event, created, stage_changed = upsert_event(
        session,
        canonical_id=cid,
        event_type=event_type,
        title=title,
        description=description,
        subject=subject,
        jurisdiction=jurisdiction,
        stage=stage,
        stage_occurred_at=stage_occurred_at,
        confidence=confidence,
        document_id=matter_doc.id,
        geography_type=geography_type,
        geography=geography,
    )

    from .models import Evidence

    for ev in evidence_items:
        session.add(
            Evidence(
                event_id=event.id,
                document_id=matter_doc.id,
                passage=ev.text,
                reason=ev.reason,
                url=matter_record.source_url,
            )
        )

    return IngestResult(
        matter_external_id=external_id,
        event_id=event.id,
        canonical_id=event.canonical_id,
        created=created,
        stage_changed=stage_changed,
        stage=event.stage.value,
        used_llm=used_llm,
    )


async def ingest_recent(
    source: RecordSource,
    session: Session,
    *,
    since: datetime | None = None,
    use_llm: bool = True,
) -> list[IngestResult]:
    """Discover + ingest every matter updated since `since`."""
    external_ids = await source.discover(since=since)
    results = []
    for external_id in external_ids:
        result = await ingest_matter(source, session, external_id, use_llm=use_llm)
        results.append(result)
    return results
