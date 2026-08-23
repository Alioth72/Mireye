"""
LLM structured event extraction.

Only called on documents that passed classify.is_potentially_relevant().
The LLM's job is narrow: interpret messy text into the schema below. It is
NOT the source of truth for stage when deterministic evidence exists (see
stage_resolver.py) -- extraction['stage'] should be treated as a fallback /
cross-check, not authoritative, whenever a MatterHistory-derived stage is
available.

No API call is wired in yet (deliberately -- pick your provider/SDK first).
This module defines the contract: prompt template + response schema +
validation, so the extraction step is a drop-in once you wire the call.
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

def call_llm_extract(document_type: str, title: str | None, date: str | None, source_url: str | None, raw_text: str) -> ExtractedEvent:
    """Calls the Gemini API for structured event extraction.

    Requires GEMINI_API_KEY in the environment. Passes ExtractedEvent
    directly as response_schema -- response_mime_type alone only guarantees
    valid JSON *syntax*, not that it matches OUR schema (field names, enum
    values, nesting). response_schema constrains the model's output to
    actually conform, instead of just hoping the prompt was followed.

    Raises RuntimeError if the key is missing, the call fails, or the
    response doesn't validate against ExtractedEvent -- callers (ingest.py)
    should catch this and fall back to the deterministic-only / heuristic
    path rather than crash the whole ingest run on one bad document.
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