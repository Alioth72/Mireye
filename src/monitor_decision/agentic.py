from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .models import AlertDecision, DecisionRequest
from .scoring import score_decision


AGENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["alert", "review", "quiet"]},
        "materiality_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "impact_direction": {"type": "string", "enum": ["positive", "negative", "mixed", "unknown"]},
        "headline": {"type": "string"},
        "rationale": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 6},
        "quiet_reason": {"type": ["string", "null"]},
        "next_best_action": {"type": ["string", "null"]},
    },
    "required": [
        "decision",
        "materiality_score",
        "confidence",
        "impact_direction",
        "headline",
        "rationale",
        "quiet_reason",
        "next_best_action",
    ],
}


SYSTEM_PROMPT = """You are Mireye Monitor's final materiality agent.
Decide whether a public-record land-use event is material at one monitored coordinate.

Rules:
- Use only the provided public-record event, deterministic score pack, and Mireye fields.
- Never invent citations, fields, dates, or jurisdiction facts.
- Preserve the event stage exactly; proposed/heard/adopted are materially different.
- Alert only when the event would plausibly change option value at this coordinate before ordinary news would.
- Stay quiet when the physical facts show the land could not respond to the event.
- Use review when scope, stage, or required Mireye fields are too uncertain.
- Keep rationale short, specific, and tied to the fields.
"""


@dataclass(frozen=True)
class AgentConfig:
    enabled: bool = False
    provider: str = "auto"
    openai_model: str = "gpt-5.6-terra"
    gemini_model: str = "gemini-3.7-flash"
    timeout_s: int = 25

    @classmethod
    def from_env(cls, *, enabled: bool = False, provider: str = "auto") -> "AgentConfig":
        return cls(
            enabled=enabled,
            provider=provider,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-terra"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.7-flash"),
            timeout_s=int(os.getenv("MONITOR_LLM_TIMEOUT_S", "25")),
        )


def decide(request: DecisionRequest, config: AgentConfig | None = None) -> dict[str, Any]:
    config = config or AgentConfig()
    deterministic = score_decision(request).as_dict()
    if not config.enabled:
        deterministic["decision_source"] = "rules"
        deterministic["agent"] = {"enabled": False, "provider": None, "model": None}
        return deterministic

    provider_order = _provider_order(config.provider)
    last_error = None
    for provider in provider_order:
        try:
            raw = _call_provider(provider, request, deterministic, config)
            model_decision = _parse_agent_json(raw)
            final = _apply_guardrails(model_decision, deterministic)
            final["citations"] = deterministic["citations"]
            final["required_fields"] = deterministic["required_fields"]
            final["missing_fields"] = deterministic["missing_fields"]
            final["score_breakdown"] = deterministic["score_breakdown"]
            final["event_stage"] = deterministic["event_stage"]
            final["decision_source"] = f"llm:{provider}"
            final["agent"] = {
                "enabled": True,
                "provider": provider,
                "model": config.openai_model if provider == "openai" else config.gemini_model,
                "guardrails_applied": final.pop("_guardrails_applied", []),
            }
            return final
        except (RuntimeError, ValueError, urllib.error.URLError, TimeoutError) as exc:
            last_error = str(exc)
            continue

    deterministic["decision_source"] = "rules_fallback"
    deterministic["agent"] = {
        "enabled": True,
        "provider": None,
        "model": None,
        "error": last_error or "no configured provider succeeded",
    }
    return deterministic


def _provider_order(provider: str) -> list[str]:
    if provider == "openai":
        return ["openai"]
    if provider == "gemini":
        return ["gemini"]
    return ["openai", "gemini"]


def _call_provider(provider: str, request: DecisionRequest, deterministic: dict[str, Any], config: AgentConfig) -> str:
    if provider == "openai":
        return _call_openai(request, deterministic, config)
    if provider == "gemini":
        return _call_gemini(request, deterministic, config)
    raise RuntimeError(f"unknown provider: {provider}")


def _call_openai(request: DecisionRequest, deterministic: dict[str, Any], config: AgentConfig) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    body = {
        "model": config.openai_model,
        "input": [
            {"role": "developer", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(_agent_payload(request, deterministic), sort_keys=True)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "monitor_agent_decision",
                "strict": True,
                "schema": AGENT_SCHEMA,
            }
        },
    }
    data = _post_json(
        "https://api.openai.com/v1/responses",
        body,
        headers={"authorization": f"Bearer {api_key}"},
        timeout_s=config.timeout_s,
    )
    if "output_text" in data:
        return str(data["output_text"])
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and "text" in content:
                return str(content["text"])
    raise RuntimeError("OpenAI response did not include output text")


def _call_gemini(request: DecisionRequest, deterministic: dict[str, Any], config: AgentConfig) -> str:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY is not set")

    prompt = SYSTEM_PROMPT + "\nReturn only JSON matching this schema:\n" + json.dumps(AGENT_SCHEMA)
    prompt += "\n\nPayload:\n" + json.dumps(_agent_payload(request, deterministic), sort_keys=True)
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.gemini_model}:generateContent"
    data = _post_json(url, body, headers={"x-goog-api-key": api_key}, timeout_s=config.timeout_s)
    try:
        return str(data["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Gemini response did not include text") from exc


def _agent_payload(request: DecisionRequest, deterministic: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": request.raw.get("event", {}),
        "site": request.raw.get("site", {}),
        "mireye_fields": {
            name: {
                "value": field.value,
                "unit": field.unit,
                "source": field.source,
                "source_url": field.source_url,
            }
            for name, field in request.fields.items()
        },
        "deterministic_decision": deterministic,
    }


def _parse_agent_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    missing = set(AGENT_SCHEMA["required"]) - set(data)
    if missing:
        raise ValueError(f"agent response missing required keys: {sorted(missing)}")
    return data


def _apply_guardrails(model_decision: dict[str, Any], deterministic: dict[str, Any]) -> dict[str, Any]:
    final = dict(model_decision)
    applied = []
    missing_fields = deterministic.get("missing_fields", [])
    deterministic_score = int(deterministic.get("materiality_score", 0))

    if len(missing_fields) >= 3 and final["decision"] == AlertDecision.ALERT.value:
        final["decision"] = AlertDecision.REVIEW.value
        final["headline"] = "Review: model wanted alert, but too many required Mireye fields are missing."
        final["next_best_action"] = "Fetch missing Mireye fields before sending an alert."
        applied.append("downgraded_alert_for_missing_fields")

    if deterministic_score < 25 and final["decision"] == AlertDecision.ALERT.value:
        final["decision"] = AlertDecision.REVIEW.value
        final["headline"] = "Review: model wanted alert, but deterministic physical materiality was very low."
        final["next_best_action"] = "Human-review the physical constraints before notifying."
        applied.append("downgraded_alert_for_low_physical_score")

    final["materiality_score"] = max(0, min(100, int(final["materiality_score"])))
    final["_guardrails_applied"] = applied
    return final


def _post_json(url: str, body: dict[str, Any], timeout_s: int, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {"content-type": "application/json", **(headers or {})}
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))
