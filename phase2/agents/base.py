"""Agent primitives: the shared state, the budget, and the contract every agent obeys.

The hierarchy exists because a single fixed fetch cannot answer this problem well. What
you should ask second depends on what came back first: `intersects_protected_area: True`
is meaningless until you also have `protected_area_gap_status`; transmission `absent`
means nothing until you have checked a second source. Both of those were real bugs, and
both are adaptive-investigation problems rather than field-list problems.

**Division of labour, and it is deliberate:**

* Agents decide *what to look at next*. They never compute a score.
* Deterministic code does all fetching and all scoring. It stays reproducible, which is
  what makes weight calibration mean anything.
* No agent may overturn a computed number. The Verifier can flag disagreement; it cannot
  edit the score.

Everything is bounded. `Budget` caps steps, fetches and model calls, because an
investigation loop that decides its own stopping condition will occasionally decide not
to stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Budget:
    """Hard ceilings. An agent that exhausts its budget stops; it does not ask twice."""

    # A full run with two adaptive rounds records roughly: begin, plan, scan, score,
    # (scout, top-up, score) x rounds, verify, analyse, end. The default must cover
    # that or the cap fires on bookkeeping rather than on real work -- an earlier
    # value of 8 overran to 9/8 on an ordinary two-round investigation.
    max_steps: int = 16
    max_fetches: int = 4
    max_model_calls: int = 6

    steps_used: int = 0
    fetches_used: int = 0
    model_calls_used: int = 0
    overruns: list = field(default_factory=list)

    def can_step(self) -> bool:
        return self.steps_used < self.max_steps

    def can_fetch(self) -> bool:
        return self.fetches_used < self.max_fetches

    def can_call_model(self) -> bool:
        return self.model_calls_used < self.max_model_calls

    def note_overrun(self, kind: str) -> None:
        """Record a ceiling being crossed rather than crossing it silently.

        Terminal bookkeeping (a coordinator's final `end` record) still has to be
        written even when the budget is spent, so the honest thing is to mark it, not
        to drop the record or pretend the cap held.
        """
        if kind not in self.overruns:
            self.overruns.append(kind)

    def summary(self) -> dict:
        out = {
            "steps": f"{self.steps_used}/{self.max_steps}",
            "fetches": f"{self.fetches_used}/{self.max_fetches}",
            "model_calls": f"{self.model_calls_used}/{self.max_model_calls}",
        }
        if self.overruns:
            out["overruns"] = self.overruns
        return out


@dataclass
class Step:
    """One recorded action. The trace is the point — an investigation nobody can read
    back is indistinguishable from a guess."""

    agent: str
    action: str
    rationale: str
    model: Optional[str] = None
    result: Optional[Any] = None
    at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "action": self.action,
            "rationale": self.rationale,
            "model": self.model,
            "at": self.at.isoformat(),
            "result": self.result,
        }


@dataclass
class Investigation:
    """Shared state threaded through every agent in one run."""

    site_id: str
    label: str
    lat: float
    lng: float
    goal: str = "data_center_optionality"

    fields_fetched: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)

    scores: dict = field(default_factory=dict)
    vicinity: dict = field(default_factory=dict)
    analysis: Optional[dict] = None
    verification: Optional[dict] = None
    stopped_because: Optional[str] = None

    def record(self, agent: str, action: str, rationale: str,
               *, model: Optional[str] = None, result: Any = None) -> Step:
        step = Step(agent=agent, action=action, rationale=rationale,
                    model=model, result=result)
        self.steps.append(step)
        self.budget.steps_used += 1
        if self.budget.steps_used > self.budget.max_steps:
            self.budget.note_overrun("steps")
        if model:
            self.budget.model_calls_used += 1
            if self.budget.model_calls_used > self.budget.max_model_calls:
                self.budget.note_overrun("model_calls")
        return step

    def note(self, text: str) -> None:
        if text not in self.findings:
            self.findings.append(text)

    def ask(self, question: str) -> None:
        if question not in self.open_questions:
            self.open_questions.append(question)

    def resolve(self, question: str) -> None:
        if question in self.open_questions:
            self.open_questions.remove(question)

    def to_dict(self) -> dict:
        return {
            "site_id": self.site_id,
            "label": self.label,
            "goal": self.goal,
            "fields_fetched": self.fields_fetched,
            "open_questions": self.open_questions,
            "findings": self.findings,
            "scores": self.scores,
            "analysis": self.analysis,
            "verification": self.verification,
            "stopped_because": self.stopped_because,
            "budget": self.budget.summary(),
            "trace": [s.to_dict() for s in self.steps],
        }


class Agent(Protocol):
    """Every agent declares its cost tier up front.

    `model_tier` is `None` for deterministic agents — most of the work here needs no
    model at all, and saying so explicitly keeps anyone from reaching for one by reflex.
    """

    name: str
    model_tier: Optional[str]

    async def run(self, inv: Investigation, **kwargs) -> Investigation:
        ...
