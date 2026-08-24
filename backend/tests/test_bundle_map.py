"""(event_type, subject) -> bundle selection. `subject` is LLM-written free text, not an
enum, so this must never guess toward the narrower/cheaper bundle when ambiguous."""

from __future__ import annotations

from monitor_records.models import EventType
from phase3.bundle_map import bundles_for, metric_for


def test_data_center_moratorium_gets_full_bundle_including_telecom():
    bundles = bundles_for(EventType.MORATORIUM, "data centers")
    assert set(bundles) == {"grid", "telecom", "terrain", "water", "constraints"}


def test_bess_moratorium_excludes_telecom():
    """Fiber is decisive for a data-centre moratorium and noise for a BESS one -- this is
    why Phase 1 keeps `subject` on the emitted event (D9)."""
    bundles = bundles_for(EventType.MORATORIUM, "battery storage facility")
    assert set(bundles) == {"grid", "terrain", "water", "constraints"}
    assert "telecom" not in bundles


def test_ambiguous_or_missing_subject_falls_back_to_the_safe_superset():
    """No subject (e.g. the heuristic fallback path never populates it) must not silently
    pick the cheaper BESS-shaped bundle -- that would risk missing fiber, which is
    decisive for a data-center moratorium."""
    assert set(bundles_for(EventType.MORATORIUM, None)) == {"grid", "telecom", "terrain", "water", "constraints"}
    assert set(bundles_for(EventType.MORATORIUM, "industrial development")) == {
        "grid", "telecom", "terrain", "water", "constraints"
    }


def test_rezoning_bundle():
    assert set(bundles_for(EventType.REZONING, None)) == {"terrain", "water", "constraints", "access"}


def test_utility_extension_power_vs_water():
    assert set(bundles_for(EventType.UTILITY_EXTENSION, "power line extension")) == {"grid", "terrain", "access"}
    assert set(bundles_for(EventType.UTILITY_EXTENSION, "sewer main extension")) == {"terrain", "water", "access"}
    # ambiguous -- fetch the union, don't guess
    assert set(bundles_for(EventType.UTILITY_EXTENSION, "infrastructure extension")) == {
        "grid", "terrain", "water", "access"
    }


def test_no_bundle_selection_ever_touches_the_parcel_record():
    from phase2.bundles import fields_for, touches_parcel_record

    for event_type in EventType:
        for subject in (None, "data centers", "battery storage", "power", "sewer"):
            fields = fields_for(bundles_for(event_type, subject))
            assert not touches_parcel_record(fields), (event_type, subject)


def test_metric_selection_matches_bundle_selection():
    assert metric_for(EventType.MORATORIUM, "data centers") == "data_center_optionality"
    assert metric_for(EventType.MORATORIUM, "battery storage") == "bess_optionality"
    assert metric_for(EventType.REZONING, None) == "buildability"
