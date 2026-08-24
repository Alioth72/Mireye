"""The Phase 3 output contract: does this event matter at this site, and why -- with
both halves of the evidence (government record + physical fact) traceable back to source.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Decision = Literal["ALERT", "SILENCE"]


class MaterialityDecision(BaseModel):
    decision: Decision
    canonical_id: str
    event_id: Optional[str] = None
    stage: str
    site_id: str

    reasons: list[str] = Field(default_factory=list)

    # Government half of the evidence -- passed through from Phase 1's Event.evidence.
    government_evidence: list[dict[str, Any]] = Field(default_factory=list)

    # Physical half -- present only once the geography gate has passed and a bundle was
    # actually evaluated (a stage/confidence/geography SILENCE never spends a Mireye call).
    metric: Optional[str] = None
    score: Optional[float] = None
    physical_components: dict[str, Any] = Field(default_factory=dict)

    replayed: bool = False  # True when this is a cached decision, not a fresh evaluation
    evaluated_at: datetime
