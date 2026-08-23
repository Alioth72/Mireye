"""Reviewer agents — Verifier and Analyst.

Both wrap an existing module behind the `Agent` protocol and both are strictly
downstream of the numbers. Neither may change a score.

* **Verifier** asks Mireye's own synthesizer the same question the arithmetic answered,
  and flags disagreement. It exists because `intersects_protected_area: True` scored a
  municipal golf course as disqualifying while the prose said GAP 4 is nominal.
* **Analyst** reads the finished facts and says what is decisive, what varies, what is
  uncertain. It produces no verdict; that is Phase 3's job.

Model tiers differ because the work differs — see `analyst.MODELS`.
"""

from __future__ import annotations

from typing import Any, Optional

from .. import analyst as analyst_mod
from .. import verify as verify_mod
from ..mireye.client import MireyeClient
from .base import Investigation


class Verifier:
    """Challenges extreme component scores against a narrative second opinion."""

    name = "verifier"
    model_tier = "mireye_ask"   # Mireye's own planner+synthesizer, not an OpenAI tier

    def __init__(self, client: MireyeClient, *, limit: int = 2):
        self.client = client
        self.limit = limit

    async def run(self, inv: Investigation, **kwargs: Any) -> Investigation:
        result = inv.scores.get(inv.goal)
        if not result:
            inv.record(self.name, "skipped", "nothing scored yet")
            return inv
        if not inv.budget.can_call_model():
            inv.record(self.name, "skipped", "model budget exhausted")
            return inv

        checked = await verify_mod.verify_site(
            self.client, inv.lat, inv.lng, result, limit=self.limit
        )
        inv.verification = checked
        inv.budget.model_calls_used += max(1, checked.get("checked", 0))

        for c in checked.get("checks", []):
            if c["agreement"] == "disputed":
                inv.note(
                    f"DISPUTED {c['component']}: scored {c['numeric_score']} but the "
                    f"narrative disagrees -- {c['note']}"
                )
                inv.ask(f"reconcile {c['component']} against the narrative")

        inv.record(
            self.name, "cross_check",
            "challenged the most extreme components against Mireye's synthesizer; "
            "flags disagreement, never edits the score",
            model="mireye/ask",
            result={"checked": checked.get("checked"), "verdict": checked.get("verdict")},
        )
        return inv


class Analyst:
    """Turns finished facts into analysis. Deliberately produces no verdict."""

    name = "analyst"
    model_tier = "site_analysis"

    def __init__(self, executor, *, model: Optional[str] = None):
        self.executor = executor
        self.model = model

    async def run(self, inv: Investigation, **kwargs: Any) -> Investigation:
        if not inv.budget.can_call_model():
            inv.record(self.name, "skipped", "model budget exhausted")
            return inv

        facts = analyst_mod.site_facts(
            inv.label,
            self.executor.datapoints(inv.site_id),
            vicinity=inv.vicinity,
            scores=inv.scores,
        )
        model = self.model or analyst_mod.model_for("site_analysis")
        try:
            inv.analysis = analyst_mod.analyse_site(
                facts, model=model,
                question="; ".join(inv.open_questions[:5]) if inv.open_questions else "",
            )
        except analyst_mod.AnalystUnavailable as exc:
            inv.record(self.name, "unavailable", str(exc))
            return inv

        inv.budget.model_calls_used += 1
        for flag in (inv.analysis.get("data_quality_flags") or []):
            inv.note(f"{flag.get('flag')}: {flag.get('detail')}")

        inv.record(
            self.name, "analyse",
            "structured analysis over the finished facts -- no verdict, by design",
            model=model,
            result={"headline": inv.analysis.get("headline")},
        )
        return inv
