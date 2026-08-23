"""Phase 2 agent hierarchy.

    Coordinator            owns the loop and the stopping condition; no model
    |
    +-- Scout              decides WHAT to look at next; mostly deterministic rules
    +-- Executor           the only agent that touches Mireye; no model, ever
    +-- Verifier           challenges extreme scores against Mireye's synthesizer
    +-- Analyst            says what is decisive/uncertain; produces no verdict

Two rules hold the design together:

1. **Agents choose what to investigate. Deterministic code fetches and scores.**
   Numbers stay reproducible, which is what makes weight calibration mean anything.
2. **No agent may overturn a computed score.** Reviewers flag disagreement; they do
   not edit arithmetic.
"""

from .base import Agent, Budget, Investigation, Step
from .coordinator import Coordinator, investigate_site
from .executor import Executor
from .reviewers import Analyst, Verifier
from .scout import Scout

__all__ = [
    "Agent", "Budget", "Investigation", "Step",
    "Coordinator", "investigate_site",
    "Executor", "Scout", "Verifier", "Analyst",
]
