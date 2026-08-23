"""
Deterministic stage resolution.

Per the spec: legal/status state must be resolved from reliable structured
evidence, not inferred by an LLM. This module only looks at Legistar's own
status fields and recorded actions -- no LLM involved.

If a matter's status can't be confidently mapped, we return None and the
caller should fall back to the LLM extraction path (which must still cite
evidence and must never be the sole source of an ADOPTED determination
without a human-checkable action string backing it up).
"""

from __future__ import annotations

from .models import EventStage

# MatterStatusName values observed on Legistar instances are inconsistent
# across clients, so this is a starting map to expand once you've pulled
# real Seattle data (scripts/probe_legistar.py) and seen the actual strings
# in use.
_STATUS_MAP: dict[str, EventStage] = {
    "introduced": EventStage.PROPOSED,
    "referred": EventStage.PROPOSED,
    "in committee": EventStage.PROPOSED,
    "held": EventStage.PROPOSED,
    "heard in committee": EventStage.HEARD,
    "public hearing held": EventStage.HEARD,
    "recommended for passage": EventStage.HEARD,
    "full council agenda ready": EventStage.HEARD,
    "passed": EventStage.ADOPTED,
    "passed as amended": EventStage.ADOPTED,
    "adopted": EventStage.ADOPTED,
    "enacted": EventStage.ADOPTED,
    "signed": EventStage.ADOPTED,
    "failed": EventStage.REJECTED,
    "rejected": EventStage.REJECTED,
    "withdrawn": EventStage.WITHDRAWN,
    "tabled": EventStage.TABLED,
}

# Action strings from the MatterHistory endpoint. Also non-exhaustive --
# expand from real data. `passed_flag` from the API (MatterHistoryPassedFlag)
# is the strongest signal when present and should be checked first by
# resolve_from_history.
_ACTION_MAP: dict[str, EventStage] = {
    "passed": EventStage.ADOPTED,
    "adopted": EventStage.ADOPTED,
    "enacted": EventStage.ADOPTED,
    "public hearing": EventStage.HEARD,
    "hearing held": EventStage.HEARD,
    "recommended for passage": EventStage.HEARD,
    "referred": EventStage.PROPOSED,
    "introduced": EventStage.PROPOSED,
    "failed": EventStage.REJECTED,
    "withdrawn": EventStage.WITHDRAWN,
    "tabled": EventStage.TABLED,
}


def resolve_from_matter_status(matter_status_name: str | None) -> EventStage | None:
    """Resolve stage from a Matter's own MatterStatusName field."""
    if not matter_status_name:
        return None
    return _STATUS_MAP.get(matter_status_name.strip().lower())


def resolve_from_history_action(
    action_name: str | None, passed_flag: bool | None = None
) -> EventStage | None:
    """Resolve stage from a single MatterHistory row.

    passed_flag (MatterHistoryPassedFlag) is Legistar's own boolean for
    "this action recorded a passing vote" -- when present it's more reliable
    than string-matching the action name.
    """
    if passed_flag:
        return EventStage.ADOPTED
    if not action_name:
        return None
    key = action_name.strip().lower()
    if key in _ACTION_MAP:
        return _ACTION_MAP[key]
    # loose substring fallback for verbose action strings, e.g.
    # "CB 121214 was passed by a vote of 9-0" contains "passed"
    for needle, stage in _ACTION_MAP.items():
        if needle in key:
            return stage
    return None


def latest_stage_from_history(
    history_rows: list[dict],
) -> tuple[EventStage | None, dict | None]:
    """Given a matter's ordered (or unordered) history rows, return the
    furthest-progressed stage reached and the row that produced it.

    Rows are expected to have 'action_name'/'passed_flag'/'action_date' keys
    (matching the metadata shape produced by SeattleLegistarSource.fetch).
    """
    from .models import STAGE_ORDER

    best_stage: EventStage | None = None
    best_row: dict | None = None
    for row in history_rows:
        stage = resolve_from_history_action(
            row.get("action_name"), row.get("passed_flag")
        )
        if stage is None:
            continue
        if best_stage is None or STAGE_ORDER[stage] > STAGE_ORDER[best_stage]:
            best_stage = stage
            best_row = row
    return best_stage, best_row
