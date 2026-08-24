"""Which stage/confidence combinations are even worth evaluating physically.

This is the cheapest possible gate -- no DB, no Mireye call, just the event dict already
in hand -- so it runs first. It directly encodes two of the four "conservative by design"
requirements: distinguishing a proposal/hearing from an actual decided outcome, and never
alerting off a low-confidence (effectively keyword-only) extraction.

Answers Phase 1's open cross-phase question (context/phase1.md sec. 8, "reconcile stage
vocabulary"): Phase 3 keeps all six of Phase 1's stages rather than collapsing them --
REJECTED and WITHDRAWN/TABLED are genuinely different outcomes and collapsing them upstream
would destroy information. Phase 3 owns the alert-eligibility judgement instead.
"""

from __future__ import annotations

from monitor_records.models import EventStage

# Only a DECIDED, terminal outcome is worth spending a physical evaluation on. REJECTED is
# included deliberately: Phase 1's own reasoning is that a rejected moratorium restores
# option value at a site, which is a legitimate positive-direction alert, not a non-event.
# PROPOSED/HEARD are not yet decided. WITHDRAWN/TABLED are non-events for materiality
# purposes -- nothing was adopted and nothing was rejected either.
ALERT_ELIGIBLE_STAGES: frozenset[EventStage] = frozenset({EventStage.ADOPTED, EventStage.REJECTED})

# Below this, treat the extraction as too uncertain to act on. 0.4 is the exact value
# ingest.py's heuristic fallback path (classify.guess_event_type, no LLM) hardcodes for
# every event it produces (see monitor_records/ingest.py) -- so this threshold specifically
# excludes "keyword-only matches" from ever reaching ALERT, per the stated requirement.
MIN_CONFIDENCE = 0.6


def confidence_bucket(confidence: float) -> str:
    """Phase 1 asked Phase 3 to own the float -> bucket mapping (context/phase1.md sec. 8,
    "agree the confidence type"). This is that answer. Used as part of the dedup key so a
    later, materially more confident re-extraction of the same (canonical_id, stage) is
    treated as a genuinely new decision rather than replaying a stale one."""
    if confidence < 0.6:
        return "low"
    if confidence < 0.85:
        return "medium"
    return "high"


def stage_gate(event: dict) -> tuple[bool, str | None]:
    """Returns (silence, reason). `silence=True` means: stop here, do not spend a Mireye
    call, the decision is already SILENCE for a reason that has nothing to do with
    physical facts."""
    stage = EventStage(event["stage"])
    if stage not in ALERT_ELIGIBLE_STAGES:
        return True, (
            f"stage is {stage.value}, not a decided outcome "
            "(only ADOPTED/REJECTED are alert-eligible; proposals, hearings, "
            "withdrawals, and tabled items are not material events)"
        )
    confidence = event["confidence"]
    if confidence < MIN_CONFIDENCE:
        return True, (
            f"confidence {confidence:.2f} is below the {MIN_CONFIDENCE} threshold "
            "(heuristic/keyword-only extraction or low-confidence LLM output; "
            "routed to review, not auto-alerted)"
        )
    return False, None
