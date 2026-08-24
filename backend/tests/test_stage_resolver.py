from monitor_records.models import EventStage
from monitor_records.stage_resolver import (
    latest_stage_from_history,
    resolve_from_history_action,
    resolve_from_matter_status,
)


def test_matter_status_proposed():
    assert resolve_from_matter_status("Introduced") == EventStage.PROPOSED
    assert resolve_from_matter_status("In Committee") == EventStage.PROPOSED


def test_matter_status_adopted():
    assert resolve_from_matter_status("Passed") == EventStage.ADOPTED
    assert resolve_from_matter_status("Passed as Amended") == EventStage.ADOPTED


def test_matter_status_unknown_returns_none():
    assert resolve_from_matter_status("Some Weird Custom Status") is None
    assert resolve_from_matter_status(None) is None


def test_history_action_passed_flag_wins():
    # even with an ambiguous action name, an explicit passed_flag is authoritative
    assert resolve_from_history_action("Discussed", passed_flag=True) == EventStage.ADOPTED


def test_history_action_substring_match():
    assert (
        resolve_from_history_action("CB 121214 was passed by a vote of 9-0")
        == EventStage.ADOPTED
    )


def test_never_infers_adoption_from_hearing():
    stage = resolve_from_history_action("Public Hearing held")
    assert stage == EventStage.HEARD
    assert stage != EventStage.ADOPTED


def test_latest_stage_from_history_picks_furthest_progress():
    rows = [
        {"action_name": "Introduced", "passed_flag": False},
        {"action_name": "Public Hearing held", "passed_flag": False},
        {"action_name": "Referred", "passed_flag": False},  # out of order, shouldn't regress
    ]
    stage, row = latest_stage_from_history(rows)
    assert stage == EventStage.HEARD
    assert row["action_name"] == "Public Hearing held"


def test_latest_stage_from_history_reaches_adopted():
    rows = [
        {"action_name": "Introduced", "passed_flag": False},
        {"action_name": "Public Hearing held", "passed_flag": False},
        {"action_name": "Final vote", "passed_flag": True},
    ]
    stage, _ = latest_stage_from_history(rows)
    assert stage == EventStage.ADOPTED


def test_latest_stage_from_history_empty():
    stage, row = latest_stage_from_history([])
    assert stage is None
    assert row is None
