"""
LLM structured event extraction.

Only called on documents that passed classify.is_potentially_relevant().
The LLM's job is narrow: interpret messy text into the schema below. It is
NOT the source of truth for stage when deterministic evidence exists (see
stage_resolver.py) -- extraction['stage'] should be treated as a fallback /
cross-check, not authoritative, whenever a MatterHistory-derived stage is
available.

Provider is isolated behind call_llm_extract() (D7 in context/phase1.md) --
ingest.py depends only on the ExtractedEvent contract, never on provider
specifics. Two providers are wired: Gemini (_call_gemini_extract) and OpenAI
(_call_openai_extract), both driven by the same SYSTEM_PROMPT/USER_PROMPT_TEMPLATE
so the extraction *behavior* doesn't drift between them -- only the API call does.

Provider selection: LLM_PROVIDER env var ("gemini" | "openai") if set; otherwise
auto-detected by which key is present, preferring OpenAI (the configured
GEMINI_API_KEY is free-tier, 20 requests/day, and was exhausted in practice by
routine testing -- OpenAI has no such ceiling on this account). If the selected
provider's call fails and the other provider's key is also configured, it's used
as an automatic fallback -- see call_llm_extract().
"""

from __future__ import annotations

import json
import os

from pydantic import BaseModel, Field, ValidationError

from .models import EventStage, EventType

SYSTEM_PROMPT = """You are extracting structured legislative event data from a \
government record for the city of Seattle. You must be conservative and \
evidence-driven.

Rules:
- Distinguish a PROPOSAL from an ADOPTION. A first reading, introduction, or \
committee referral is PROPOSED, not ADOPTED.
- Distinguish a HEARING from a PASSAGE. A public hearing being held is HEARD, \
not ADOPTED, even if the hearing went well.
- Do NOT infer that something was adopted/passed unless the text contains \
explicit evidence of a final vote or enactment.
- Identify the activity/subject materially affected (e.g. "data centers", \
"industrial zoning in SODO").
- Identify geographic scope as precisely as the text supports. If the text \
does not support a specific location, scope is the jurisdiction (Seattle) \
as a whole -- do not guess a neighborhood.
- Cite the exact passage(s) that justify each field you fill in.
- If evidence is insufficient for a field, return null/false for it rather \
than guessing.
- Never fabricate facts not present in the source text.

Return ONLY a JSON object matching the schema you were given. No prose, no \
markdown fences.
"""

USER_PROMPT_TEMPLATE = """Document type: {document_type}
Document title: {title}
Document date: {date}
Source: {source_url}

--- DOCUMENT TEXT ---
{raw_text}
--- END DOCUMENT TEXT ---

Extract the event information as JSON.
"""


class ExtractedEvidence(BaseModel):
    text: str = Field(description="Exact quoted passage from the source text")
    reason: str = Field(description="Why this passage supports the extraction")


class ExtractedGeography(BaseModel):
    type: str = Field(description="JURISDICTION | POINT | POLYGON | UNRESOLVED")
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class ExtractedEvent(BaseModel):
    is_material_event: bool
    event_type: EventType | None = None
    title: str | None = None
    description: str | None = None
    subject: str | None = None
    stage: EventStage | None = None
    geographic_scope: ExtractedGeography | None = None
    evidence: list[ExtractedEvidence] = Field(default_factory=list)
    confidence: float = 0.0
GEMINI_RESPONSE_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "is_material_event": {"type": "BOOLEAN"},
        "event_type": {
            "type": "STRING",
            "enum": [e.value for e in EventType],
            "nullable": True,
        },
        "title": {"type": "STRING", "nullable": True},
        "description": {"type": "STRING", "nullable": True},
        "subject": {"type": "STRING", "nullable": True},
        "stage": {
            "type": "STRING",
            "enum": [s.value for s in EventStage],
            "nullable": True,
        },
        "geographic_scope": {
            "type": "OBJECT",
            "nullable": True,
            "properties": {
                "type": {"type": "STRING", "description": "JURISDICTION | POINT | POLYGON | UNRESOLVED"},
                "name": {"type": "STRING", "nullable": True},
                "latitude": {"type": "NUMBER", "nullable": True},
                "longitude": {"type": "NUMBER", "nullable": True},
            },
            "required": ["type"],
        },
        "evidence": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "text": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                },
                "required": ["text", "reason"],
            },
        },
        "confidence": {"type": "NUMBER"},
    },
    "required": ["is_material_event", "confidence"],
}

def build_prompt(document_type: str, title: str | None, date: str | None, source_url: str | None, raw_text: str) -> str:
    return USER_PROMPT_TEMPLATE.format(
        document_type=document_type,
        title=title or "(untitled)",
        date=date or "(unknown)",
        source_url=source_url or "(none)",
        raw_text=raw_text,
    )

_PROVIDER_FNS = {"gemini": "_call_gemini_extract", "openai": "_call_openai_extract"}


def call_llm_extract(document_type: str, title: str | None, date: str | None, source_url: str | None, raw_text: str) -> ExtractedEvent:
    """Provider-selecting entry point. See module docstring for selection order.

    If the selected provider's call fails (including a rate limit -- discovered in
    practice, not hypothetically: the configured GEMINI_API_KEY is on the free tier,
    20 requests/day, and testing this exact code path exhausted it) AND the other
    provider has a key configured, this falls back to the other provider before giving
    up -- one real LLM being temporarily unavailable shouldn't force a drop all the way
    to the keyword heuristic when a second one is right there. Only if every configured
    provider fails does this raise, which callers (ingest.py) should catch and fall back
    to the deterministic-only / heuristic path rather than crash the whole ingest run on
    one bad document.
    """
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if provider and provider not in _PROVIDER_FNS:
        raise RuntimeError(f"unknown LLM_PROVIDER {provider!r}; expected 'gemini' or 'openai'")

    if not provider:
        # OpenAI preferred: the configured GEMINI_API_KEY is free-tier (20 requests/day)
        # and was exhausted by routine testing; OpenAI has no such ceiling on this
        # account. Gemini remains a real, working fallback (see below), just not primary.
        if os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        elif os.environ.get("GEMINI_API_KEY"):
            provider = "gemini"
        else:
            provider = "openai"  # default error message when neither is set

    fallback = "openai" if provider == "gemini" else "gemini"
    fallback_key = "OPENAI_API_KEY" if fallback == "openai" else "GEMINI_API_KEY"
    # Only auto-fallback when LLM_PROVIDER wasn't explicitly pinned -- an explicit
    # pin (e.g. to compare providers, or because one is known-bad) must be honored
    # exactly, not silently overridden by availability.
    allow_fallback = not os.environ.get("LLM_PROVIDER", "").strip() and bool(os.environ.get(fallback_key))

    primary_fn = globals()[_PROVIDER_FNS[provider]]
    try:
        return primary_fn(document_type, title, date, source_url, raw_text)
    except Exception as primary_exc:
        if not allow_fallback:
            raise
        fallback_fn = globals()[_PROVIDER_FNS[fallback]]
        try:
            return fallback_fn(document_type, title, date, source_url, raw_text)
        except Exception as fallback_exc:
            raise RuntimeError(
                f"both LLM providers failed -- {provider}: {primary_exc}; {fallback}: {fallback_exc}"
            ) from fallback_exc


def _call_gemini_extract(document_type: str, title: str | None, date: str | None, source_url: str | None, raw_text: str) -> ExtractedEvent:
    """Calls the Gemini API for structured event extraction.

    Requires GEMINI_API_KEY in the environment. Passes ExtractedEvent
    directly as response_schema -- response_mime_type alone only guarantees
    valid JSON *syntax*, not that it matches OUR schema (field names, enum
    values, nesting). response_schema constrains the model's output to
    actually conform, instead of just hoping the prompt was followed.
    """
    import google.generativeai as genai  # local import: keep this an optional dependency

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set; cannot run LLM extraction")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        "gemini-2.5-flash",
        system_instruction=SYSTEM_PROMPT,
    )
    prompt = build_prompt(document_type, title, date, source_url, raw_text)

    response = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            response_schema=GEMINI_RESPONSE_SCHEMA,
            # Structured legislative-event extraction should be reproducible, not
            # creative -- unset temperature defaults to sampling, which means the
            # self-reported `confidence` (and in principle any other field) can vary
            # between two calls on the identical prompt. That's a real source of the
            # "confidence is not calibrated" risk already flagged (R5): it's not just
            # uncalibrated, it isn't even stable run-to-run without this.
            temperature=0.0,
        ),
    )

    raw = (response.text or "").strip()
    # strip stray markdown fences defensively, even with response_schema set
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM did not return valid JSON: {e}\nRaw response: {raw[:500]}")

    try:
        return ExtractedEvent.model_validate(parsed)
    except ValidationError as e:
        raise RuntimeError(f"LLM JSON did not match ExtractedEvent schema: {e}")


def _call_openai_extract(document_type: str, title: str | None, date: str | None, source_url: str | None, raw_text: str) -> ExtractedEvent:
    """Calls the OpenAI API for structured event extraction.

    Requires OPENAI_API_KEY in the environment. Same SYSTEM_PROMPT/USER_PROMPT_TEMPLATE
    as the Gemini path (_call_gemini_extract) -- only the API call differs, per D7
    ("provider isolated behind one function"). Uses chat.completions.parse() with
    ExtractedEvent passed directly as response_format: the SDK derives the JSON schema
    from the Pydantic model and returns an already-parsed, already-validated instance,
    so (unlike the Gemini path, whose SDK needed a hand-built schema -- see
    GEMINI_RESPONSE_SCHEMA -- because it couldn't auto-convert Pydantic's "default" keys)
    there's no manual JSON parsing step here.
    """
    from openai import OpenAI  # local import: keep this an optional dependency

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set; cannot run LLM extraction")

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key)
    prompt = build_prompt(document_type, title, date, source_url, raw_text)

    completion = client.chat.completions.parse(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format=ExtractedEvent,
        # Same reasoning as the Gemini path: structured extraction should be
        # reproducible, not creative.
        temperature=0.0,
    )

    message = completion.choices[0].message
    if message.refusal:
        raise RuntimeError(f"OpenAI refused the extraction request: {message.refusal}")
    if message.parsed is None:
        raise RuntimeError("OpenAI did not return a parsed ExtractedEvent")
    return message.parsed