"""Phase 3 HTTP surface: one endpoint, POST /v1/decide -- named to match the contract
Phase 2's own ideation doc already assumed ("That combining step is Phase 3's
POST /v1/decide and it stays there.").
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from monitor_records.api import _event_to_json
from monitor_records.db import get_session as get_p1_session
from monitor_records.models import Event

from phase2.db import get_session as get_p2_session
from phase2.mireye.client import MireyeClient
from phase2.mireye.schemas import MireyeError

from .db import get_session as get_p3_session
from .pipeline import decide
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
