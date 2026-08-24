"""Phase 3 HTTP surface.

POST /v1/decide is the one endpoint that can *make* a decision -- named to match the
contract Phase 2's own ideation doc already assumed ("That combining step is Phase 3's
POST /v1/decide and it stays there."). Everything else here is a read of what that
endpoint already wrote: GET /v1/decisions and GET /v1/replay/runs exist so a console can
render the decision history without the act of looking at it re-running the pipeline or
spending a credit.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from monitor_records.api import _event_to_json
from monitor_records.db import get_session as get_p1_session
from monitor_records.models import Event

from phase2.db import get_session as get_p2_session
from phase2.mireye.client import MireyeClient
from phase2.mireye.schemas import MireyeError

from .db import get_session as get_p3_session
from .models import P3Decision
# `_from_row` is the pipeline's own stored-row -> MaterialityDecision projection, the
# same one it uses to replay a deduped decision. The read endpoints below reuse it
# rather than re-deriving the mapping, so a replayed decision and a listed decision
# cannot drift apart field by field.
from .pipeline import _from_row, decide
from .schemas import MaterialityDecision

router = APIRouter(prefix="/v1", tags=["phase3"])


async def get_client() -> AsyncIterator[MireyeClient]:
    client = MireyeClient()
    async with client:
        yield client


class DecideRequest(BaseModel):
    event_id: Optional[str] = None
    event: Optional[dict[str, Any]] = None
    site_id: str


@router.post("/decide", response_model=MaterialityDecision, tags=["phase3"])
async def post_decide(
    body: DecideRequest,
    p2_session: Session = Depends(get_p2_session),
    p3_session: Session = Depends(get_p3_session),
    client: MireyeClient = Depends(get_client),
) -> MaterialityDecision:
    if not body.event_id and not body.event:
        raise HTTPException(status_code=400, detail={"error": "no_event", "message": "send event_id or event"})

    if body.event is not None:
        event = body.event
    else:
        with get_p1_session() as p1_session:  # type: Session
            row = p1_session.get(Event, body.event_id)
            if row is None:
                raise HTTPException(status_code=404, detail={"error": "event_not_found", "message": body.event_id})
            event = _event_to_json(row)

    try:
        return await decide(event, body.site_id, p2_session=p2_session, p3_session=p3_session, client=client)
    except MireyeError as exc:
        status = exc.status_code or (503 if exc.retryable else 500)
        raise HTTPException(
            status_code=status,
            detail={"error": exc.code, "message": exc.message, "retryable": exc.retryable},
        ) from exc


# --------------------------------------------------------------------------
# console read surface -- pure reads of p3_decision; no pipeline, no credits
# --------------------------------------------------------------------------
@router.get("/decisions", response_model=list[MaterialityDecision], tags=["phase3"])
def list_decisions(
    canonical_id: Optional[str] = None,
    site_id: Optional[str] = None,
    decision: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    p3_session: Session = Depends(get_p3_session),
) -> list[MaterialityDecision]:
    """Stored decisions, newest first.

    This is a SELECT against `p3_decision` and nothing else. It does NOT call
    `pipeline.decide()`, does not read a Phase 1 event, does not open a MireyeClient, and
    therefore cannot spend a credit or write a row -- which is the entire reason the
    endpoint exists. A console needs to show "what has this system decided so far", and
    the one thing it must never do is answer that question by deciding again: re-running
    the pipeline to render a list would spend real money on already-answered questions
    and, worse, would blur the line between a decision the system made and a decision the
    act of looking at it produced.

    `limit` is capped at 1000 rather than unbounded so a console mistake cannot pull the
    whole table into memory; `canonical_id` / `site_id` / `decision` narrow the same
    query server-side instead of making the caller filter a truncated page client-side.

    Rows come back as `MaterialityDecision` because that is already the Phase 3 output
    contract -- a listed decision and a POST /v1/decide response are the same object.
    `replayed` is therefore True on every row here (nothing was freshly evaluated), and
    `event_id` is null: `p3_decision` keys on `canonical_id`, deliberately, since one
    government action may arrive as several event rows and must still be one decision.
    """
    query = select(P3Decision).order_by(P3Decision.decided_at.desc()).limit(limit)
    if canonical_id:
        query = query.where(P3Decision.canonical_id == canonical_id)
    if site_id:
        query = query.where(P3Decision.site_id == site_id)
    if decision:
        query = query.where(P3Decision.decision == decision)
    return [_from_row(row) for row in p3_session.exec(query).all()]


@router.get("/replay/runs", tags=["phase3"])
def replay_runs(p3_session: Session = Depends(get_p3_session)) -> dict[str, Any]:
    """The lead-time scorecard -- reporting only what the stored decisions actually support.

    The headline claim this system wants to make is a lead-time one: alerted N days
    before adoption, M days before the first press coverage. Neither number is derivable
    from `p3_decision`. That table records what was decided and when it was decided; it
    holds no adoption date and no press date, and no replay corpus of historical events
    (with those dates attached) exists yet. So the honest answer to "what is the lead
    time" is "not measurable yet", and this endpoint says exactly that: `corpus` is
    explicitly null and `note` states the missing input in plain words.

    It returns 200, not an error, because "no corpus yet" is a true and complete answer
    to a well-formed question -- a console should render an empty state, not an outage.

    What IS derivable from the stored rows is reported and nothing more: how many
    decisions exist, how they split by decision value and by stage, and how many distinct
    government actions they cover. There is deliberately no precision, recall, or
    false-positive rate here: those require ground-truth labels the system has never been
    given, and a fabricated number on a scorecard is worse than an absent one, because a
    reader cannot tell the two apart once it has been rendered.
    """
    total = p3_session.exec(select(func.count()).select_from(P3Decision)).one()
    by_decision = {
        value: count
        for value, count in p3_session.exec(
            select(P3Decision.decision, func.count()).group_by(P3Decision.decision)
        ).all()
    }
    by_stage = {
        value: count
        for value, count in p3_session.exec(
            select(P3Decision.stage, func.count()).group_by(P3Decision.stage)
        ).all()
    }
    distinct_canonical_ids = p3_session.exec(
        select(func.count(func.distinct(P3Decision.canonical_id)))
    ).one()

    return {
        "total_decisions": total,
        "by_decision": by_decision,
        "by_stage": by_stage,
        "distinct_canonical_ids": distinct_canonical_ids,
        # Explicitly null, not omitted and not zero: there is no replay corpus, and a
        # missing key would read as a bug while a 0 would read as a measured result.
        "corpus": None,
        "note": (
            "No replay corpus exists yet. Lead time versus adoption and lead time versus "
            "first press coverage cannot be computed until a replay corpus with adoption "
            "dates and press dates exists. The counts above are derived only from stored "
            "decisions; no precision, recall, or lead-time figure is reported because "
            "none can be computed from the data on hand."
        ),
    }
