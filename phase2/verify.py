"""Narrative cross-check against `/v1/ask`.

Phase 2's score is deterministic and must stay that way: reproducible, auditable, and
stable enough that weight calibration means something. So an LLM never feeds the score.
It sits *beside* it, answering the same question in prose, and we flag disagreement.

This exists because of a real bug. `intersects_protected_area: True` at Interbay scored
the site 0.046 — hard-disqualified — when the land was a municipal golf course under
PAD-US GAP 4. Asked the same question, Mireye's own synthesizer said:

    "GAP Status 4 — the lowest and least restrictive tier... carries no mandate to
     prevent land conversion... does not constitute a meaningful legal barrier to
     development."

A cross-check would have caught that on day one.

**The flag is a heuristic; the narrative is the deliverable.** `agreement` is derived
from keyword negation and is deliberately conservative — it exists to draw a human's
eye, never to overrule arithmetic. Always surface `answer` alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Optional

from .mireye.client import MireyeClient
from .mireye.schemas import MireyeError

#: Phrases indicating the narrative sees NO real constraint. Matched only when we
#: scored a constraint as blocking, so a false positive costs a review, not a decision.
_NOT_A_BARRIER = (
    "not constitute a meaningful legal barrier",
    "no mandate to prevent",
    "least restrictive",
    "does not prevent",
    "not a development constraint",
    "minimal real constraint",
    "nominal",
    "does not restrict",
    "no legal barrier",
)

#: Phrases indicating the narrative sees a real constraint we may have scored as clear.
_IS_A_BARRIER = (
    "prohibits development",
    "cannot be developed",
    "strictly protected",
    "wilderness",
    "legally protected from",
    "precludes development",
    "significant barrier",
)

#: Targeted questions per component. The planner caps at 15 fields, so these are
#: deliberately narrow -- "assess this site" would truncate and answer vaguely.
QUESTIONS: dict = {
    "clear": "Is this land legally protected from development? "
             "State the PAD-US GAP status and what it does or does not restrict.",
    "power": "What is the nearest transmission line voltage and distance, "
             "and how far is the nearest substation?",
    "btm_fuel": "Could an on-site gas generator be permitted here? "
                "Consider air-quality nonattainment and any nearby Class I area.",
    "terrain": "Is this ground buildable? Consider slope, landslide susceptibility, "
               "seismic design category and karst.",
    "water": "Is municipal water service available here, and is the watershed stressed?",
}


@dataclass
class CrossCheck:
    component: str
    numeric_score: float
    basis: str
    question: str
    answer: str = ""
    confidence: Optional[str] = None
    data_gaps: list = dc_field(default_factory=list)
    fields_used: list = dc_field(default_factory=list)
    agreement: str = "inconclusive"   # aligned | disputed | inconclusive
    note: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "component": self.component,
            "numeric_score": self.numeric_score,
            "numeric_basis": self.basis,
            "question": self.question,
            "answer": self.answer,
            "ask_confidence": self.confidence,
            "data_gaps": self.data_gaps,
            "fields_used": self.fields_used,
            "agreement": self.agreement,
            "note": self.note,
            "error": self.error,
        }


def _judge(component: str, numeric: float, answer: str) -> tuple:
    """Compare a prose answer against a numeric component score.

    Deliberately narrow, and deliberately timid. Two properties matter:

    * **One-sided evidence only.** Synthesiser prose varies between calls -- the same
      question can return "GAP 4, least restrictive" once and mention "protected area"
      more prominently the next. When both directions appear we return "inconclusive"
      rather than letting whichever list is checked first decide.
    * **Only the two directions that have actually bitten**: scoring a nominal
      constraint as blocking (the Interbay golf course), and scoring a real constraint
      as clear. Everything else is left alone.
    """
    text = answer.lower()
    clear_hits = [p for p in _NOT_A_BARRIER if p in text]
    block_hits = [p for p in _IS_A_BARRIER if p in text]

    if clear_hits and block_hits:
        return "inconclusive", (
            "narrative cites both a constraint and a reason it does not bind -- "
            "read the answer rather than the flag"
        )
    if not clear_hits and not block_hits:
        return "inconclusive", "narrative neither confirms nor contradicts the score"

    narrative_says_clear = bool(clear_hits)

    if numeric <= 0.35 and narrative_says_clear:
        return "disputed", (
            "scored as blocking, but the narrative describes no real constraint -- "
            "check the qualifier fields before trusting the low score"
        )
    if numeric >= 0.85 and not narrative_says_clear:
        return "disputed", (
            "scored as unconstrained, but the narrative describes a real barrier -- "
            "a qualifier may be missing from the fetch"
        )
    if numeric <= 0.35:
        return "aligned", "narrative confirms the constraint"
    if numeric >= 0.85:
        return "aligned", "narrative confirms no meaningful constraint"
    return "inconclusive", "score is mid-range; a narrative check adds little"


async def cross_check_component(
    client: MireyeClient,
    lat: float,
    lng: float,
    component: str,
    numeric_score: float,
    basis: str,
) -> CrossCheck:
    """Ask one targeted question and compare it against one component score."""
    question = QUESTIONS.get(
        component, f"Assess the {component} characteristics of this location."
    )
    result = CrossCheck(component=component, numeric_score=numeric_score,
                        basis=basis, question=question)
    try:
        data = await client.ask(question, lat=lat, lng=lng)
    except MireyeError as exc:
        result.error = f"{exc.code}: {exc.message}"
        return result

    result.answer = data.get("answer", "") or ""
    result.confidence = data.get("confidence")
    # `data_gaps` is authoritative -- computed from the fetch result, not the prose.
    result.data_gaps = data.get("data_gaps") or []
    result.fields_used = data.get("fields_used") or []
    result.agreement, result.note = _judge(component, numeric_score, result.answer)

    if result.confidence == "low":
        result.note += " (ask confidence low: >30% of planner fields came back null)"
    return result


def components_worth_checking(score_result: dict, *, limit: int = 3) -> list:
    """Pick the components most worth a narrative check.

    Extremes are where errors hide: a near-zero score claims something is impossible and
    a near-one claims nothing stands in the way. Middling scores rarely flip a decision.
    """
    candidates = []
    for name, comp in score_result.get("components", {}).items():
        if name not in QUESTIONS:
            continue
        sc = comp.get("score")
        if sc is None:
            continue
        extremity = max(0.35 - sc, sc - 0.85)
        if extremity > 0:
            candidates.append((extremity, name, sc, comp.get("basis", "")))
    candidates.sort(reverse=True)
    return [(n, s, b) for _, n, s, b in candidates[:limit]]


async def verify_site(
    client: MireyeClient,
    lat: float,
    lng: float,
    score_result: dict,
    *,
    limit: int = 3,
) -> dict:
    """Cross-check a scored site. Never mutates the score."""
    checks = []
    for name, sc, basis in components_worth_checking(score_result, limit=limit):
        checks.append(await cross_check_component(client, lat, lng, name, sc, basis))

    disputed = [c for c in checks if c.agreement == "disputed"]
    return {
        "metric": score_result.get("metric"),
        "score": score_result.get("score"),
        "checked": len(checks),
        "disputed": len(disputed),
        "verdict": "disputed" if disputed else ("aligned" if checks else "not_checked"),
        "checks": [c.to_dict() for c in checks],
        "note": (
            "The narrative never feeds the score — it sits beside it. `agreement` is a "
            "conservative keyword heuristic meant to draw attention; read `answer`."
        ),
    }
