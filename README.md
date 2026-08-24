# Mireye Monitor Decision Layer

This package is the final layer of the monitoring agent:

1. Takes a structured public-record event from the watcher.
2. Takes Mireye field-level facts for the monitored coordinate or vicinity.
3. Scores whether the event is physically material at that site.
4. Emits either an alert, a review-needed decision, or a stay-quiet decision with cited reasons.

It is intentionally dependency-light so it can run in a hackathon stack, a background worker, or behind a small HTTP service.

## Quick Start

Run a local scoring example:

```bash
PYTHONPATH=src python3 -m monitor_decision score --input examples/data_center_moratorium_request.json --pretty
```

Run agentic adjudication. The system tries OpenAI first, then Gemini if `--provider auto` is used:

```bash
export OPENAI_API_KEY="..."
export GEMINI_API_KEY="..."
PYTHONPATH=src python3 -m monitor_decision score --input examples/data_center_moratorium_request.json --agentic --provider auto --pretty
```

Run the simple HTTP service:

```bash
PYTHONPATH=src python3 -m monitor_decision.server --port 8080
```

Then call:

```bash
curl -s http://localhost:8080/v1/decide \
  -H 'content-type: application/json' \
  --data @examples/data_center_moratorium_request.json
```

For agentic HTTP mode, add these top-level keys to the JSON body:

```json
{
  "decision_mode": "agentic",
  "llm_provider": "auto"
}
```

Replay historical records:

```bash
PYTHONPATH=src python3 -m monitor_decision replay --input examples/replay.jsonl --pretty
```

## Input Contract

The decision layer expects one JSON request:

```json
{
  "event": {
    "id": "seattle-2026-06-09-dc-moratorium",
    "type": "data_center_moratorium",
    "stage": "adopted",
    "title": "Emergency moratorium on new data centers",
    "jurisdiction": "Seattle, WA",
    "published_at": "2026-06-09T22:15:00Z",
    "source_url": "https://example.gov/agenda",
    "source_quote": "Council adopted an emergency moratorium on new data centers.",
    "scope": {
      "relation_to_site": "inside",
      "distance_m": 0,
      "description": "City limits"
    }
  },
  "site": {
    "id": "site-001",
    "lat": 47.6062,
    "lng": -122.3321,
    "label": "Monitored coordinate"
  },
  "mireye": {
    "fields": {
      "nearest_transmission_line_voltage_kv": {"value": 230, "source": "EIA Energy Atlas"},
      "nearest_transmission_line_distance_m": {"value": 1100, "unit": "m", "source": "EIA Energy Atlas"},
      "nearest_substation_distance_m": {"value": 2600, "unit": "m", "source": "EIA Energy Atlas"},
      "fiber_broadband_available": {"value": true},
      "slope_degrees": {"value": 2.1},
      "within_floodplain_polygon": {"value": false},
      "intersects_wetland": {"value": false},
      "intersects_protected_area": {"value": false}
    }
  }
}
```

The `mireye.fields` values can be plain values or Mireye-style field objects with `value`, `unit`, `source`, `source_url`, `confidence`, and `fetched_at`.

## Decisions

- `alert`: material and citeable enough to notify.
- `review`: potentially material, but missing fields or ambiguous scope/stage.
- `quiet`: not material at this coordinate based on the known facts.

See [docs/design-brief.md](docs/design-brief.md) for the teammate-facing integration brief.
