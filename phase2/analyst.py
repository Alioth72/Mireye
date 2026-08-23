"""LLM analysis layer — structure in the facts, not a verdict on them.

Phase 2's scores are useful but they are not the decision, and they should not pretend
to be. A number like `0.911` reads as a judgement ("this land is 91% good") when what
Phase 3 actually needs is the reasoning material: what is decisive here, what varies
across a set and what does not, what we do not know, and what would change the picture.

**This module never decides.** There is no `verdict`, no `recommendation`, no
`good`/`bad`, no alert/quiet. The output schema deliberately has nowhere to put one. If
a future field starts to smell like a conclusion, that is the boundary leaking.

It also never feeds the score. The deterministic path stays reproducible and auditable —
which is what makes weight calibration meaningful — and this sits beside it.

Why an LLM at all: the useful observations here are comparative and contextual. *"Every
site in this set has 115–230 kV within 2 km, so power is not what separates them; the only
dimension with real range is distance from settlement"* is not something a threshold
produces. It is a reading of a table.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# model routing
# ---------------------------------------------------------------------------
# Different tasks need different reasoning depth, and paying top-tier rates for a
# bounded summarisation task is waste. Route by what the task actually demands:
#
#   set_analysis   -- comparative reasoning across 20+ sites x 50 fields each. Has to
#                     hold the whole table, spot which dimensions vary and which are
#                     constant, and tell "these places are alike" from "these places
#                     were badly chosen". This is the one that earns a frontier model.
#   site_analysis  -- one site, bounded context, largely reading values off a block.
#                     A mid-tier model does this well.
#   triage         -- short mechanical judgements (is this field name relevant, does
#                     this note indicate a coverage problem). Cheapest tier.
#
# Anything fully mechanical should not call a model at all -- coverage artefacts,
# staleness and absent-vs-missing are already computed deterministically in
# vicinity.py and store.py, and an LLM would only add cost and variance.
MODELS: dict = {
    "set_analysis": "gpt-5",
    "site_analysis": "gpt-5-mini",
    "triage": "gpt-5-nano",
}


def model_for(task: str) -> str:
    """Resolve a task to a model, allowing per-task env override.

    PHASE2_MODEL_SET_ANALYSIS / _SITE_ANALYSIS / _TRIAGE
    """
    return os.environ.get(f"PHASE2_MODEL_{task.upper()}") or MODELS.get(
        task, MODELS["site_analysis"]
    )


DEFAULT_MODEL = MODELS["site_analysis"]

#: Structured-output contract. Note what is absent: no verdict, no score, no
#: recommendation. Analysis names what is true and what is uncertain; deciding what to
#: do about it belongs to Phase 3.
ANALYSIS_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "decisive_factors", "differentiators", "uniform_factors",
                 "uncertainties", "data_quality_flags", "what_would_change_the_picture"],
    "properties": {
        "headline": {
            "type": "string",
            "description": "One sentence stating what this set of facts shows. "
                           "Descriptive, not evaluative.",
        },
        "decisive_factors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["factor", "evidence", "why_it_matters"],
                "properties": {
                    "factor": {"type": "string"},
                    "evidence": {"type": "string",
                                 "description": "Cite actual field values."},
                    "why_it_matters": {"type": "string"},
                },
            },
        },
        "differentiators": {
            "type": "array",
            "description": "Dimensions that actually vary across the set, with range.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["factor", "range", "note"],
                "properties": {
                    "factor": {"type": "string"},
                    "range": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
        },
        "uniform_factors": {
            "type": "array",
            "description": "Dimensions that are effectively constant across the set and "
                           "therefore carry no comparative signal here.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["factor", "value", "implication"],
                "properties": {
                    "factor": {"type": "string"},
                    "value": {"type": "string"},
                    "implication": {"type": "string"},
                },
            },
        },
        "uncertainties": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["what", "why_it_matters", "how_to_resolve"],
                "properties": {
                    "what": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "how_to_resolve": {"type": "string"},
                },
            },
        },
        "data_quality_flags": {
            "type": "array",
            "description": "Measurement problems: coverage artefacts, absent-vs-missing "
                           "confusion, staleness, single-source claims.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["flag", "detail"],
                "properties": {
                    "flag": {"type": "string"},
                    "detail": {"type": "string"},
                },
            },
        },
        "what_would_change_the_picture": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}

_SYSTEM = """You analyse geospatial site data. You do NOT make decisions.

Your job is to surface structure in a table of facts: what is decisive, what varies,
what is constant, what is uncertain, and what would change the reading. Another system
decides what to do about it.

Rules:
- Never say a site is good, bad, suitable, unsuitable, recommended or disqualified.
  Describe what is true and what follows from it.
- Cite actual field values as evidence. "230 kV at 1.3 km" not "strong grid access".
- A factor that is identical across every site carries NO comparative signal. Say so
  plainly -- it is often the most useful observation in the set.
- Mireye field status is tri-state. `ok` is a value, `absent` means the source
  affirmatively found nothing there (a real answer), `failed` means the fetch errored
  (not an answer). Never treat `absent` as missing data.
- Where a field is present at some sample points and absent at others, that is a
  search-radius coverage artefact, not a fact about the ground. Flag it.
- Distinguish what the data shows from what it cannot show. Parcel boundaries,
  zoning, utility willingness to serve and land price are all outside this dataset.
- Be concrete and brief. No hedging filler."""


class AnalystUnavailable(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        env = Path(".env")
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("OPENAI_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    if not key:
        raise AnalystUnavailable("OPENAI_API_KEY is not set")
    return key


def _client():
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise AnalystUnavailable("openai package not installed") from exc
    return OpenAI(api_key=_api_key())


# ---------------------------------------------------------------------------
# fact rendering
# ---------------------------------------------------------------------------
def site_facts(
    label: str,
    datapoints: list,
    *,
    vicinity: Optional[dict] = None,
    scores: Optional[dict] = None,
) -> dict:
    """Compact, provenance-preserving fact block for one site.

    Statuses travel with values because the tri-state distinction changes meaning, and
    coverage notes travel because a field present at 4 of 25 sample points is a
    different claim from one present everywhere.
    """
    fields: dict = {}
    for dp in datapoints:
        entry: dict = {"value": dp.value, "status": dp.status}
        if dp.source:
            entry["source"] = dp.source
        v = (vicinity or {}).get(dp.field_name)
        if v:
            entry["vicinity"] = {
                k: v[k] for k in ("best", "worst", "best_at_m", "fraction_usable",
                                  "coverage_note")
                if v.get(k) is not None
            }
        fields[dp.field_name] = entry

    block: dict = {"site": label, "fields": fields}
    if scores:
        block["computed_scores"] = {
            m: {"score": r["score"],
                "components": {k: {"score": c["score"], "basis": c["basis"]}
                               for k, c in r["components"].items()}}
            for m, r in scores.items()
        }
    return block


def _call(client, model: str, user_payload: str, name: str) -> dict:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": _SYSTEM},
                  {"role": "user", "content": user_payload}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": ANALYSIS_SCHEMA},
        },
    )
    return json.loads(response.choices[0].message.content)


def analyse_site(facts: dict, *, model: Optional[str] = None, question: str = "") -> dict:
    """Analyse one site. Returns structure, never a verdict.

    Mid-tier model by default: one site is a bounded read, not comparative reasoning.
    """
    model = model or model_for("site_analysis")
    prompt = (
        "Analyse this single location's geospatial facts.\n\n"
        f"{json.dumps(facts, indent=2, default=str)}\n\n"
        "With one site there is no comparative set, so leave `differentiators` and "
        "`uniform_factors` empty unless the vicinity ring itself shows internal "
        "variation worth naming."
    )
    if question:
        prompt += f"\n\nPay particular attention to: {question}"
    out = _call(_client(), model, prompt, "site_analysis")
    out["_model"] = model
    out["_scope"] = "site"
    return out


def analyse_set(
    sites: list,
    *,
    model: Optional[str] = None,
    question: str = "",
) -> dict:
    """Analyse a set comparatively.

    This is the mode that answers questions a single score cannot: whether a flat spread
    means the sites are genuinely alike or the sample was badly chosen. It carries the
    most context and the hardest reasoning, so it gets the frontier tier.
    """
    model = model or model_for("set_analysis")
    prompt = (
        f"Analyse these {len(sites)} locations as a SET.\n\n"
        f"{json.dumps(sites, indent=2, default=str)}\n\n"
        "The comparative question matters most: which dimensions actually separate "
        "these sites, and which are effectively constant across all of them and so "
        "carry no signal here. If the set looks homogeneous, say whether that is a "
        "property of the places or a property of how they were chosen."
    )
    if question:
        prompt += f"\n\nPay particular attention to: {question}"
    out = _call(_client(), model, prompt, "set_analysis")
    out["_model"] = model
    out["_scope"] = "set"
    out["_n_sites"] = len(sites)
    return out
