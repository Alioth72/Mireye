# Design Brief: Monitor Decision Layer

## Purpose

This layer decides whether a monitored public-record event is material at a specific coordinate. It does not scrape agendas, own parcel data, or run site selection. It consumes:

- A structured event from the monitoring agent.
- Mireye field facts for the monitored coordinate or vicinity.

It returns:

- `alert`, `review`, or `quiet`.
- A 0-100 materiality score.
- Stage-aware language.
- Positive/negative impact direction.
- Missing fields.
- Public-record and Mireye citations.

## API Boundary

Endpoint:

```http
POST /v1/decide
```

Minimum request:

```json
{
  "event": {
    "id": "stable-source-event-id",
    "type": "data_center_moratorium",
    "stage": "proposed",
    "title": "First reading of data-center moratorium",
    "jurisdiction": "Demo County",
    "published_at": "2026-06-09T22:15:00Z",
    "source_url": "https://county.gov/agendas/item-12",
    "source_quote": "Short excerpt proving the event and stage.",
    "scope": {
      "relation_to_site": "inside",
      "distance_m": 0,
      "description": "County limits"
    }
  },
  "site": {
    "id": "monitored-site-id",
    "lat": 47.6062,
    "lng": -122.3321,
    "label": "Optional human label"
  },
  "mireye": {
    "fields": {}
  }
}
```

Supported event types:

- `data_center_moratorium`
- `bess_moratorium`
- `rezoning`
- `annexation`
- `comp_plan_amendment`
- `utility_extension`
- `big_permit`
- `unknown`

Supported stages:

- `proposed`
- `heard`
- `adopted`
- `unknown`

Scope relation values the scorer understands:

- `inside`, `intersects`, `same_jurisdiction`, `direct`
- `adjacent`, `neighboring_jurisdiction`
- `nearby`, `buffer`
- `unknown`

## Watcher Team Contract

The watcher should emit one event only when a source item clears the keyword/classification gate. Do not call Mireye for every agenda line.

Required watcher fields:

- Stable `event.id`.
- `event.type`.
- `event.stage`, with exact stage language preserved in `source_quote`.
- `event.title`.
- `event.jurisdiction`.
- `event.source_url`.
- `event.source_quote`.
- `event.scope.relation_to_site` or enough geometry for the scope layer to compute it.

Recommended watcher fields:

- `event.detected_terms`.
- `event.published_at`.
- `event.scope.geometry_ref`, pointing to stored GeoJSON or a docket attachment.
- `event.scope.description`.

## Mireye Fetch Team Contract

Fetch only after the watcher has a scoped event. For data-center and BESS moratoria, the current scoring core expects:

- `nearest_transmission_line_voltage_kv`
- `nearest_transmission_line_distance_m`
- `nearest_substation_distance_m`
- `fiber_broadband_available` for data centers
- `slope_degrees`
- `within_floodplain_polygon`
- `intersects_wetland`
- `intersects_protected_area`

For rezoning, annexation, comp-plan amendments, and utility extensions:

- `slope_degrees`
- `within_floodplain_polygon`
- `intersects_wetland`
- `intersects_protected_area`
- `nearest_major_road_distance_m`

Field objects may be raw values or Mireye-style objects:

```json
{
  "value": 230,
  "unit": "kV",
  "source": "EIA Energy Atlas",
  "source_url": "https://source.example",
  "confidence": "HIGH",
  "fetched_at": "2026-08-22T12:00:00Z"
}
```

## Response Contract

The response is stable JSON:

```json
{
  "decision": "alert",
  "materiality_score": 71,
  "confidence": "high",
  "impact_direction": "negative",
  "event_stage": "proposed",
  "headline": "Alert: data_center_moratorium is 71/100 material here (negative impact, proposed).",
  "rationale": [],
  "quiet_reason": null,
  "missing_fields": [],
  "required_fields": [],
  "score_breakdown": {
    "event_relevance": 1.0,
    "stage_weight": 0.72,
    "scope_fit": 1.0,
    "physical_optionality": 0.99,
    "evidence_confidence": 1.0
  },
  "citations": [],
  "next_best_action": "If monitoring nearby jurisdictions, score adjacent high-optionality sites for spillover value."
}
```

## Agentic Decision Mode

The current implementation supports two modes:

- `rules`: deterministic score and decision only. This is the default.
- `agentic`: deterministic scoring first, then an LLM adjudicator makes the final decision from the event, Mireye fields, citations, missing fields, and score breakdown.

The agentic layer is intentionally hybrid:

- The rule scorer builds the evidence pack and baseline materiality.
- The model decides whether the real-world interpretation should remain `alert`, become `review`, or stay `quiet`.
- Guardrails preserve citations and event stage, and downgrade model alerts when too many required Mireye fields are missing or deterministic physical materiality is extremely low.
- If no model key is configured or a provider fails, the system falls back to the deterministic decision and marks `decision_source` as `rules_fallback`.

Provider configuration:

```bash
export OPENAI_API_KEY="..."
export OPENAI_MODEL="gpt-5.6-terra"

export GEMINI_API_KEY="..."
export GEMINI_MODEL="gemini-3.7-flash"
```

OpenAI is the intended primary provider. Gemini is available as a backup while waiting on hackathon OpenAI keys.

HTTP request switches:

```json
{
  "decision_mode": "agentic",
  "llm_provider": "auto"
}
```

CLI:

```bash
PYTHONPATH=src python3 -m monitor_decision score --input examples/data_center_moratorium_request.json --agentic --provider auto --pretty
```

## Alert Policy

The scorer intentionally treats silence as a decision:

- `alert`: score >= 62 and the core evidence is present.
- `review`: ambiguous scope, unknown stage, many missing fields, or medium materiality.
- `quiet`: low materiality after scope and physical constraints are considered.

For first readings, the alert must say `proposed`. For adoption, it may say `adopted`. Never rewrite the stage into certainty the public record does not support.

## Replay Evaluation

For the hackathon demo, replay 6-12 months of one jurisdiction:

- Alert date: first meeting/source date where the watcher would have emitted the event.
- Adoption date: final vote/effective date.
- Press date: first local-news coverage date.
- Lead time versus adoption: `adoption_date - alert_date`.
- Lead time versus press: `press_date - alert_date`.
- Precision: human-reviewed true material alerts / all alerts.
- Misses: material adopted events that never produced alert or review.

The winning demo story is not "we found rezoning." It is "we alerted at first reading, stayed quiet on non-buildable ground, and beat local press by N days."
