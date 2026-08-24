"""Derived optionality scoring. The `_clear_component` three-way status handling is the
single highest-leverage correctness detail in the whole pipeline (context/phase2.md:
"`absent` is not missing... `intersects_wetland: absent` RAISES optionality") -- get this
wrong and Phase 3 either wrongly alerts on a site that was never buildable, or wrongly
stays silent on one that was.
"""

from __future__ import annotations

from phase2.scoring import UNCERTAIN, score_metric
from tests.fakes.mireye_fake import _bad_fields, _good_fields


def _row(status: str, value=None) -> dict:
    return {"status": status, "value": value}


def test_absent_constraint_flag_reads_as_confirmed_clear():
    fields = {"intersects_wetland": _row("absent")}
    score = score_metric("buildability", fields)
    assert score.components["clear"].score == 1.0


def test_ok_true_constraint_flag_reads_as_flagged():
    fields = {"intersects_wetland": _row("ok", True)}
    score = score_metric("buildability", fields)
    assert score.components["clear"].score < 0.2


def test_ok_false_constraint_flag_reads_as_confirmed_clear():
    fields = {"intersects_wetland": _row("ok", False)}
    score = score_metric("buildability", fields)
    assert score.components["clear"].score == 1.0


def test_failed_constraint_flag_is_neither_clear_nor_flagged():
    """A failed fetch must not be silently treated as "absent" (would wrongly raise
    optionality on a real data gap) nor as "true" (would wrongly tank a good site)."""
    fields = {"intersects_wetland": _row("failed")}
    score = score_metric("buildability", fields)
    assert score.components["clear"].score == UNCERTAIN
    assert score.components["clear"].score not in (1.0, 0.05)


def test_three_statuses_on_the_same_field_produce_three_different_scores():
    absent = score_metric("buildability", {"intersects_wetland": _row("absent")}).components["clear"].score
    ok_true = score_metric("buildability", {"intersects_wetland": _row("ok", True)}).components["clear"].score
    failed = score_metric("buildability", {"intersects_wetland": _row("failed")}).components["clear"].score
    assert len({absent, ok_true, failed}) == 3


def test_good_and_bad_site_profiles_land_on_opposite_sides_of_alert_threshold():
    """This is the demo's whole premise: two physically different sites must produce two
    genuinely different scores, not just different raw field values."""
    ALERT_THRESHOLD = 0.5
    good = score_metric("data_center_optionality", _good_fields())
    bad = score_metric("data_center_optionality", _bad_fields())
    assert good.score >= ALERT_THRESHOLD
    assert bad.score < ALERT_THRESHOLD


def test_fiber_is_absent_from_bess_optionality():
    """Fiber is decisive for a data-centre moratorium and noise for a BESS one -- this is
    why Phase 1 keeps `subject` on the emitted event (D9)."""
    score = score_metric("bess_optionality", _good_fields())
    assert "fiber" not in score.components


def test_unknown_metric_raises():
    import pytest

    with pytest.raises(KeyError):
        score_metric("data_center_moratorium", {})
