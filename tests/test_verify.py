"""Narrative cross-check verdicts.

`_judge` is a deliberately timid heuristic. Synthesiser prose varies between calls, so
the same question can come back worded differently and a naive first-match rule would
give a non-deterministic verdict. These tests pin the two directions that have actually
bitten, and pin the timidity everywhere else.
"""

from __future__ import annotations

import pytest

from phase2.verify import QUESTIONS, _judge, components_worth_checking

# The real Mireye answer for Interbay, abridged. This is the prose that would have
# caught the golf-course bug on day one.
INTERBAY_ANSWER = (
    "The coordinate does intersect a PAD-US designated protected area (designation: LP, "
    "managed by SPR), but the protection level is GAP Status 4 — the lowest and least "
    "restrictive tier. GAP 4 carries no mandate to prevent land conversion (typical "
    "examples include city parks and military installations), so this designation does "
    "not constitute a meaningful legal barrier to development."
)

WILDERNESS_ANSWER = (
    "This coordinate falls inside a federally designated wilderness area, which "
    "prohibits development of any permanent structure."
)


# ==========================================================================
# the direction that actually bit
# ==========================================================================
def test_blocking_score_against_a_permissive_narrative_is_disputed() -> None:
    """REGRESSION. Interbay scored 0.046 on the bare `intersects_protected_area` flag.
    The narrative says GAP 4 is nominal. That disagreement is the whole point."""
    agreement, note = _judge("clear", 0.05, INTERBAY_ANSWER)
    assert agreement == "disputed"
    assert "no real constraint" in note


def test_corrected_score_against_the_same_narrative_is_aligned() -> None:
    """After the GAP-status fix the same site scores 0.95, and the same prose now
    agrees. A cross-check that flagged both would be useless."""
    agreement, _ = _judge("clear", 0.95, INTERBAY_ANSWER)
    assert agreement == "aligned"


def test_clear_score_against_a_real_barrier_is_disputed() -> None:
    """The other direction: we scored the ground as unconstrained and the narrative
    describes wilderness. A qualifier is probably missing from the fetch."""
    agreement, note = _judge("clear", 0.95, WILDERNESS_ANSWER)
    assert agreement == "disputed"
    assert "real barrier" in note


def test_blocking_score_against_a_real_barrier_is_aligned() -> None:
    assert _judge("clear", 0.05, WILDERNESS_ANSWER)[0] == "aligned"


# ==========================================================================
# timidity
# ==========================================================================
def test_mixed_evidence_is_inconclusive_not_a_coin_flip() -> None:
    """Prose citing BOTH a constraint and a reason it does not bind must not resolve to
    whichever keyword list happens to be checked first — that would make the verdict
    depend on generation wording rather than on the site."""
    mixed = ("The site is strictly protected wilderness in part, though the GAP 4 "
             "portion carries no mandate to prevent conversion.")
    agreement, note = _judge("clear", 0.05, mixed)
    assert agreement == "inconclusive"
    assert "read the answer" in note


def test_silent_narrative_is_inconclusive() -> None:
    """An answer about something else entirely must never produce a verdict."""
    agreement, _ = _judge("clear", 0.05, "The nearest transmission line is 1.2 km away.")
    assert agreement == "inconclusive"


@pytest.mark.parametrize("score", [0.4, 0.5, 0.6, 0.8])
def test_mid_range_scores_are_left_alone(score: float) -> None:
    """A cross-check earns its keep at the extremes. A 0.6 that shifts to 0.55 does not
    flip a decision, so flagging it is noise."""
    assert _judge("clear", score, INTERBAY_ANSWER)[0] == "inconclusive"


def test_empty_answer_never_disputes() -> None:
    assert _judge("clear", 0.05, "")[0] == "inconclusive"


# ==========================================================================
# selection
# ==========================================================================
def test_only_extreme_components_are_checked() -> None:
    """Errors hide at the extremes: a near-zero claims something is impossible, a
    near-one claims nothing stands in the way."""
    result = {"components": {
        "clear": {"score": 0.05, "basis": "protected"},
        "power": {"score": 0.95, "basis": "230 kV"},
        "terrain": {"score": 0.60, "basis": "slope 12 deg"},
        "water": {"score": 0.50, "basis": "unknown"},
    }}
    picked = [name for name, _, _ in components_worth_checking(result)]
    assert set(picked) == {"clear", "power"}


def test_most_extreme_component_is_checked_first() -> None:
    result = {"components": {
        "clear": {"score": 0.30, "basis": ""},
        "power": {"score": 0.02, "basis": ""},
    }}
    assert components_worth_checking(result)[0][0] == "power"


def test_selection_respects_the_limit() -> None:
    result = {"components": {n: {"score": 0.01, "basis": ""} for n in QUESTIONS}}
    assert len(components_worth_checking(result, limit=2)) == 2


def test_components_without_a_question_are_skipped() -> None:
    """Only components with a targeted question are checkable — the planner caps at 15
    fields, so a vague catch-all question would truncate and answer nothing useful."""
    result = {"components": {"carbon": {"score": 0.01, "basis": ""},
                             "clear": {"score": 0.01, "basis": ""}}}
    assert [n for n, _, _ in components_worth_checking(result)] == ["clear"]


def test_penalty_components_with_no_score_do_not_crash() -> None:
    result = {"components": {"clear": {"score": None, "basis": ""}}}
    assert components_worth_checking(result) == []
