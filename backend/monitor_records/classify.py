"""
Cheap deterministic relevance filter.

Runs BEFORE any LLM call. Goal: cut the volume of documents that get sent to
the LLM extraction step down to only those plausibly about a material event,
per the spec ("do NOT create an expensive LLM-reads-everything architecture").

This is intentionally a blunt keyword filter, not a classifier model. It
should be over-inclusive (false positives are cheap -- one more LLM call;
false negatives are expensive -- a missed event).
"""

from __future__ import annotations

import re

from .models import EventType

_KEYWORDS = [
    "moratorium",
    "rezon",  # rezone, rezoning
    "annex", 
    "zoning", # annexation
    "comprehensive plan",
    "comp plan",
    "utility extension",
    "infrastructure",
    "major development",
    "permit",
    "industrial",
    "data center",
    "battery storage",
    "warehouse",
]

_PATTERN = re.compile("|".join(re.escape(k) for k in _KEYWORDS), re.IGNORECASE)

# Document types that are structurally unlikely to carry substantive content
# (e.g. bare procedural history rows) -- skip these regardless of keywords to
# avoid burning LLM calls on "Referred to committee" one-liners. Adjust once
# you've seen real data volumes.
_SKIP_DOCUMENT_TYPES = {"attachment"}  # attachments need text extraction first; see risk #1


def is_potentially_relevant(title: str | None, raw_text: str | None, document_type: str) -> bool:
    if document_type in _SKIP_DOCUMENT_TYPES:
        return False
    haystack = " ".join(filter(None, [title, raw_text]))
    if not haystack:
        return False
    return bool(_PATTERN.search(haystack))


# Ordered so the most specific keyword groups are checked first -- a title
# containing both "moratorium" and generic "permit" language should classify
# as MORATORIUM, not the broad MAJOR_DEVELOPMENT_PERMIT bucket.
_EVENT_TYPE_KEYWORDS: list[tuple[re.Pattern, EventType]] = [
    (re.compile(r"moratorium", re.IGNORECASE), EventType.MORATORIUM),
    (re.compile(r"rezon|zoning", re.IGNORECASE), EventType.REZONING),
    (re.compile(r"annex", re.IGNORECASE), EventType.ANNEXATION),
    (re.compile(r"comprehensive plan|comp plan", re.IGNORECASE), EventType.COMP_PLAN_AMENDMENT),
    (re.compile(r"utility extension|water main extension|sewer extension", re.IGNORECASE), EventType.UTILITY_EXTENSION),
    (
        re.compile(
            r"major development|data center|battery storage|warehouse|industrial (?:permit|development)",
            re.IGNORECASE,
        ),
        EventType.MAJOR_DEVELOPMENT_PERMIT,
    ),
]


def guess_event_type(title: str | None, raw_text: str | None) -> EventType | None:
    """Cheap keyword-based EventType guess.

    This is a fallback/bridge for when the LLM extraction step (extract.py)
    isn't wired up or fails -- it lets the ingest pipeline still produce a
    typed Event. When the LLM extraction *is* available, prefer its
    event_type; this heuristic is intentionally coarse (e.g. every permit
    mention lands in MAJOR_DEVELOPMENT_PERMIT regardless of actual scale).
    """
    haystack = " ".join(filter(None, [title, raw_text]))
    if not haystack:
        return None
    for pattern, event_type in _EVENT_TYPE_KEYWORDS:
        if pattern.search(haystack):
            return event_type
    return None
