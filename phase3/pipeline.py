"""Does this event matter at this site? The one function that holds both halves at once.

Order matters, cheapest checks first, since every check past the dedup lookup risks
spending real Mireye credits:

    1. dedup check                -- one cheap, indexed DB read; must run before any
                                      gate below, since every gate can persist a decision
    2. stage + confidence gate    -- free, the event dict is already in hand
    3. geography gate             -- one cheap DB read (the Site row), no Mireye call
    4. bundle fetch + score       -- the only step that can cost credits (cache-miss)

A SILENCE at any step short-circuits before the ones after it ever run.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from monitor_records.models import EventType

from phase2 import scoring
from phase2.bundles import fields_for
from phase2.models import Site
from phase2.mireye.client import MireyeClient
from phase2.orchestrator import fetch_and_store
from phase2.store import read_fields, serialize

from .bundle_map import bundles_for, metric_for
from .geography import geography_gate
from .models import P3Decision
from .schemas import MaterialityDecision
from .stage_policy import confidence_bucket, stage_gate

ALERT_THRESHOLD = 0.5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _from_row(row: P3Decision) -> MaterialityDecision:
    return MaterialityDecision(
        decision=row.decision,
        canonical_id=row.canonical_id,
        stage=row.stage,
        site_id=row.site_id,
        reasons=row.reasons,
        government_evidence=row.government_evidence,
        metric=row.metric,
        score=row.score,
        physical_components=row.components,
        replayed=True,
        evaluated_at=row.decided_at,
    )


def _persist(p3_session: Session, key: tuple[str, str, str, str], result: MaterialityDecision) -> None:
    canonical_id, stage, bucket, site_id = key
    row = P3Decision(
        canonical_id=canonical_id,
        stage=stage,
        confidence_bucket=bucket,
        site_id=site_id,
        decision=result.decision,
        reasons=result.reasons,
        metric=result.metric,
        score=result.score,
        components=result.physical_components,
        government_evidence=result.government_evidence,
    )
    p3_session.add(row)
    p3_session.commit()


def _build(
    event: dict, site_id: str, *, decision: str, reasons: list[str],
    metric: str | None = None, score: float | None = None, components: dict | None = None,
) -> MaterialityDecision:
    return MaterialityDecision(
        decision=decision,
        canonical_id=event["canonical_id"],
        event_id=event.get("event_id"),
        stage=event["stage"],
        site_id=site_id,
        reasons=reasons,
        government_evidence=event.get("evidence") or [],
        metric=metric,
        score=score,
        physical_components=components or {},
        replayed=False,
        evaluated_at=_utcnow(),
    )


async def decide(
    event: dict,
    site_id: str,
    *,
    p2_session: Session,
    p3_session: Session,
    client: MireyeClient,
) -> MaterialityDecision:
    """`event` is the Phase 1 JSON contract (monitor_records.api._event_to_json shape).
    `site_id` identifies a phase2.models.Site already registered via POST /v1/sites.
    """
    # 1. dedup -- one cheap, indexed DB read, before ANY gate. This must come first, not
    # just before the physical-evaluation step: every gate below can persist a decision
    # (a stage-gated SILENCE included), so a repeat call for the same key has to be
    # caught here or it will retry the same INSERT and hit the UNIQUE constraint.
    # Confidence is bucketed INTO the key so a later, materially more confident
    # re-extraction of the same (canonical_id, stage) is treated as a new decision
    # rather than replaying a stale one (see stage_policy.confidence_bucket).
    key = (event["canonical_id"], event["stage"], confidence_bucket(event["confidence"]), site_id)
    cached = p3_session.exec(
        select(P3Decision).where(
            P3Decision.canonical_id == key[0],
            P3Decision.stage == key[1],
            P3Decision.confidence_bucket == key[2],
            P3Decision.site_id == key[3],
        )
    ).first()
    if cached is not None:
        return _from_row(cached)

    # 2. stage + confidence gate -- free
    silence, reason = stage_gate(event)
    if silence:
        result = _build(event, site_id, decision="SILENCE", reasons=[reason])
        _persist(p3_session, key, result)
        return result

    # 3. geography gate -- needs the Site row, no Mireye call yet
    site = p2_session.get(Site, site_id)
    if site is None:
        result = _build(event, site_id, decision="SILENCE", reasons=[f"site {site_id} not found"])
        _persist(p3_session, key, result)
        return result

    silence, reason = geography_gate(event, site)
    if silence:
        result = _build(event, site_id, decision="SILENCE", reasons=[reason])
        _persist(p3_session, key, result)
        return result

    # 4. only now spend Mireye credits: bundle selection, cache-miss autofetch, scoring
    event_type = EventType(event["event_type"])
    subject = event.get("subject")
    bundles = bundles_for(event_type, subject)
    fields = fields_for(bundles)

    read = read_fields(p2_session, site_id, fields)
    if read.to_fetch:
        await fetch_and_store(p2_session, client, site, read.to_fetch, trigger="cache_miss", caller_ref=event["canonical_id"])
        read = read_fields(p2_session, site_id, fields)  # re-read post-fetch

    serialized = {dp.field_name: serialize(dp) for dp in (read.answers + read.withheld)}

    metric = metric_for(event_type, subject)
    score = scoring.score_metric(metric, serialized)

    if score.score >= ALERT_THRESHOLD:
        reasons = [
            f"{metric}={score.score:.2f} >= {ALERT_THRESHOLD}: site is physically material to this event "
            f"({event['stage'].lower()} {event_type.value.lower()})",
        ]
        decision = "ALERT"
    else:
        worst_name, worst = min(score.components.items(), key=lambda kv: kv[1].score)
        reasons = [
            f"{metric}={score.score:.2f} < {ALERT_THRESHOLD}: site's physical profile means this event "
            f"does not materially change its options (weakest factor: {worst_name} - {worst.basis})",
        ]
        decision = "SILENCE"

    components = {
        name: {"score": c.score, "weight": c.weight, "basis": c.basis} for name, c in score.components.items()
    }
    result = _build(
        event, site_id, decision=decision, reasons=reasons, metric=metric, score=score.score, components=components
    )
    _persist(p3_session, key, result)
    return result
