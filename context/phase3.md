# Phase 3: Monitor Decision Layer

## Goal

Build the final materiality layer for the monitoring agent. This layer receives:

- A structured public-record event from the watcher.
- Mireye facts for the monitored coordinate or vicinity.

It returns an auditable final decision:

- `alert`
- `review`
- `quiet`

The product point is not keyword matching. It is deciding whether this specific ground can physically respond to the event.

## Current Architecture

The implementation is hybrid and agentic:

1. A deterministic rules engine computes the baseline evidence pack.
2. An optional LLM adjudicator reviews the event, Mireye fields, citations, missing fields, and score breakdown.
3. Guardrails preserve the event stage and citations, and downgrade unsafe model alerts.
4. If model keys are missing or provider calls fail, the system falls back to the deterministic decision.

Default mode is rules-only. Agentic mode is opt-in.

## Entry Points

CLI:

```bash
PYTHONPATH=src python3 -m monitor_decision score \
  --input examples/data_center_moratorium_request.json \
  --pretty
```

Agentic CLI:

```bash
PYTHONPATH=src python3 -m monitor_decision score \
  --input examples/data_center_moratorium_request.json \
  --agentic \
  --provider auto \
  --pretty
```

HTTP service:

```bash
PYTHONPATH=src python3 -m monitor_decision.server --port 8080
```

Decision endpoint:

```http
POST /v1/decide
```

To enable the model adjudicator over HTTP, include these top-level keys:

```json
{
  "decision_mode": "agentic",
  "llm_provider": "auto"
}
```

## Provider Configuration

OpenAI is the intended primary provider:

```bash
export OPENAI_API_KEY="..."
export OPENAI_MODEL="gpt-5.6-terra"
```

Gemini is the current fallback while waiting for OpenAI hackathon keys:

```bash
export GEMINI_API_KEY="..."
export GEMINI_MODEL="gemini-3.7-flash"
```

Provider behavior:

- `--provider openai`: OpenAI only.
- `--provider gemini`: Gemini only.
- `--provider auto`: OpenAI first, then Gemini.

## Watcher Contract

The watcher should emit one normalized event only after the source item clears the relevance gate.

Required fields:

- `event.id`
- `event.type`
- `event.stage`
- `event.title`
- `event.jurisdiction`
- `event.source_url`
- `event.source_quote`
- `event.scope.relation_to_site`
- `site.id`
- `site.lat`
- `site.lng`

Supported event stages from Phase 1:

- `PROPOSED` -> `proposed`
- `HEARD` -> `heard`
- `ADOPTED` -> `adopted`
- `REJECTED` -> `rejected`
- `WITHDRAWN` -> `withdrawn`
- `TABLED` -> `tabled`
- anything else -> `unknown`

Phase 3 keeps the six Phase 1 stages rather than collapsing them. A rejected moratorium can be a positive alert because it restores option value. Withdrawn and tabled items are treated as weak/ambiguous and normally route to review or quiet.

Supported event types:

- Phase 1 `MORATORIUM` + subject `data centers` -> `data_center_moratorium`
- Phase 1 `MORATORIUM` + subject `BESS` / battery / energy storage -> `bess_moratorium`
- `REZONING` -> `rezoning`
- `ANNEXATION` -> `annexation`
- `COMP_PLAN_AMENDMENT` -> `comp_plan_amendment`
- `UTILITY_EXTENSION` -> `utility_extension`
- `MAJOR_DEVELOPMENT_PERMIT` -> `big_permit`
- anything else -> `unknown`

## Mireye Contract

The Mireye fetch layer should run only after the watcher has found a scoped material event. Do not poll Mireye continuously.

For data-center moratoria, provide:

- `nearest_transmission_line_voltage_kv`
- `nearest_transmission_line_distance_m`
- `nearest_substation_distance_m`
- `fiber_broadband_available`
- `slope_degrees`
- `within_floodplain_polygon`
- `intersects_wetland`
- `intersects_protected_area`

For BESS moratoria, provide:

- `nearest_transmission_line_voltage_kv`
- `nearest_transmission_line_distance_m`
- `nearest_substation_distance_m`
- `slope_degrees`
- `within_floodplain_polygon`
- `intersects_wetland`
- `intersects_protected_area`

For rezoning, annexation, comp-plan amendments, and utility extensions, provide:

- `slope_degrees`
- `within_floodplain_polygon`
- `intersects_wetland`
- `intersects_protected_area`
- `nearest_major_road_distance_m`

Field values can be raw values, Mireye-style field objects, or Phase 2 datapoints. Phase 2 datapoints may arrive as:

```json
{
  "field_name": "nearest_transmission_line_voltage_kv",
  "value": 230,
  "unit": "kV",
  "status": "ok",
  "source": "EIA Energy Atlas",
  "source_url": "https://source.example",
  "license": "public-domain",
  "confidence": "high",
  "fetched_at": "2026-08-24T12:00:00Z",
  "stale": false,
  "profile": "default"
}
```

Tri-state handling is load-bearing:

- `ok`: use the value.
- `absent`: treat as a real answer; for boolean `within_*`, `intersects_*`, and `*_available` fields this means `false`.
- `failed`: treat as missing and route high-materiality cases to `review`, not `alert`.

Citations carry `status`, `license`, `confidence`, `stale`, `profile`, and source fields when present.

## Cross-Phase Resolutions

Phase 3 resolves the open asks from Phase 1 and Phase 2 as follows:

- Stage vocabulary: Phase 3 accepts all six Phase 1 stages and preserves them in output.
- Phase 1 confidence: stored on the parsed event as a float; final response confidence is Phase 3's own evidence bucket (`high`, `medium`, `low`).
- Phase 2 tri-state datapoints: `absent` is not missing; `failed` is missing.
- Licenses: citations include `license` when Phase 2 provides it.
- Derived profile names: citations include `profile` when a derived Phase 2 score/datapoint provides it.
- Same-site cache reuse: Phase 3 does not introduce event ids into Phase 2 calls; it uses the same `site.id` across event stages.

## Output Contract

The response includes:

- `decision`
- `decision_source`
- `materiality_score`
- `confidence`
- `impact_direction`
- `event_stage`
- `headline`
- `rationale`
- `quiet_reason`
- `missing_fields`
- `required_fields`
- `score_breakdown`
- `citations`
- `next_best_action`
- `agent`

`decision_source` values:

- `rules`
- `llm:openai`
- `llm:gemini`
- `rules_fallback`

## Guardrails

The model cannot:

- Invent citations.
- Change the event stage.
- Alert when three or more required Mireye fields are missing.
- Alert when deterministic physical materiality is extremely low.

Those cases become `review`.

## Replay Metrics

Historical replay is supported:

```bash
PYTHONPATH=src python3 -m monitor_decision replay --input examples/replay.jsonl --pretty
```

Replay reports:

- Precision.
- Recall.
- Misses.
- False positives.
- Average lead time versus adoption.
- Average lead time versus first press coverage.

## Verified

Current verification:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m monitor_decision replay --input examples/replay.jsonl --pretty
PYTHONPATH=src python3 -m py_compile src/monitor_decision/agentic.py src/monitor_decision/server.py src/monitor_decision/cli.py src/monitor_decision/replay.py
```

All passed locally before this context handoff was written.
