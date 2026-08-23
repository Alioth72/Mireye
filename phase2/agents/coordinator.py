"""Coordinator — runs the investigation loop and owns the stopping condition.

The loop exists because a fixed field list cannot answer this problem well: what you
should ask second depends on what came back first. Both historical bugs were failures of
exactly that. The shape is:

    Scout      plan the opening question set
    Executor   one vicinity scan (25 ring points, one batch call)
    Executor   score, deterministically
    -- loop, bounded --
      Scout    what do the first answers imply we should ask next?
      Executor fetch those qualifiers at the centroid
      Executor re-score
    Verifier   challenge the most extreme components against a narrative
    Analyst    say what is decisive, what varies, what is uncertain

**The Coordinator decides when to stop, and it is deliberately not the model's call.**
An agent asked to judge its own sufficiency will occasionally decide it needs one more
look, forever. Stopping is a budget question and a "did the last round change anything"
question, both of which are cheaper and more reliable to answer in code.
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Session

from .. import scoring
from ..models import Site
from ..mireye.client import MireyeClient
from .base import Investigation
from .executor import Executor
from .reviewers import Analyst, Verifier
from .scout import Scout


class Coordinator:
    """Top of the hierarchy. Delegates; never fetches, scores or judges itself."""

    name = "coordinator"
    model_tier = None   # routing logic, not reasoning

    def __init__(self, session: Session, client: MireyeClient):
        self.session = session
        self.client = client
        self.executor = Executor(session, client)
        self.scout = Scout()
        self.verifier = Verifier(client)
        self.analyst = Analyst(self.executor)

    async def investigate(
        self,
        site: Site,
        *,
        goal: str = "data_center_optionality",
        verify: bool = True,
        analyse: bool = True,
        max_rounds: int = 2,
        inv: Optional[Investigation] = None,
    ) -> Investigation:
        inv = inv or Investigation(
            site_id=site.id, label=site.label or site.id,
            lat=site.lat, lng=site.lng, goal=goal,
        )
        inv.record(self.name, "begin", f"goal={goal}; delegating to scout -> executor")

        if site.degraded:
            inv.note(
                "parcel_grade is false -- this coordinate may sit on a neighbouring "
                "parcel, so intrinsic facts here may describe the wrong property"
            )

        # 1. opening question set, then one vicinity scan
        opening = self.scout.initial_fields(goal)
        inv.record(self.scout.name, "plan_opening",
                   f"{len(opening)} fields implied by goal '{goal}'")
        await self.executor.scan(inv, opening, site=site)
        self.executor.score(inv, metrics=[goal])

        # 2. adaptive rounds -- the follow-ups the first answers imply
        for round_no in range(max_rounds):
            if not (inv.budget.can_step() and inv.budget.can_fetch()):
                inv.stopped_because = "budget"
                break

            await self.scout.run(inv, observed=self.executor.observed(inv.site_id))
            pending = self.scout.pending_fields(inv)
            if not pending:
                inv.stopped_because = f"no further questions after round {round_no}"
                break

            before = inv.scores.get(goal, {}).get("score")
            await self.executor.top_up(inv, pending, site=site)
            self.executor.score(inv, metrics=[goal])
            after = inv.scores.get(goal, {}).get("score")

            if before is not None and after is not None and abs(after - before) < 0.001:
                inv.note(
                    f"round {round_no + 1} qualifiers left the score unchanged "
                    f"({after:.3f}) -- they confirmed rather than corrected"
                )
                inv.stopped_because = "qualifiers changed nothing"
                break
            if before is not None and after is not None:
                inv.note(
                    f"round {round_no + 1} qualifiers moved the score "
                    f"{before:.3f} -> {after:.3f}"
                )
        else:
            inv.stopped_because = "max rounds reached"

        # 3. reviewers -- downstream of the numbers, unable to change them
        if verify:
            await self.verifier.run(inv)
        if analyse:
            await self.analyst.run(inv)

        inv.record(self.name, "end",
                   f"stopped: {inv.stopped_because or 'complete'}",
                   result=inv.budget.summary())
        return inv


async def investigate_site(
    session: Session,
    client: MireyeClient,
    site: Site,
    **kwargs,
) -> dict:
    """Convenience entry point returning the serialisable investigation."""
    coordinator = Coordinator(session, client)
    inv = await coordinator.investigate(site, **kwargs)
    return inv.to_dict()
