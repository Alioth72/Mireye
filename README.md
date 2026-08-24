# The Monitor

An agentic monitoring system: public government records → structured event (Phase 1) →
Mireye physical features (Phase 2) → materiality decision, `ALERT` or `SILENCE`
(Phase 3), conservative by design — a false alert is worse than staying silent. See
`context/phase1.md`, `context/phase2.md`, `context/phase3.md` for each phase's full
integration contract, decisions, and cross-phase asks.

## Repository layout

Two trees, **two virtual environments**. The backend carries the Mireye and LLM stack;
the frontend carries only a web server and an HTTP client. Keeping the environments
apart is not tidiness — it is what stops the console importing a phase module and
quietly bypassing the API boundary the three phases were designed around.

```
backend/     the three phases + the combined API      (cwd for backend commands)
frontend/    the operator console, map-first          (cwd for frontend commands)
context/     the per-phase decision logs — read GLOBAL.md first
Plan/        the brief, the Mireye API reference, the PAD-US bug report
```

## Quickstart — the whole pipeline, one command

Run these from `backend/`:

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pytest tests -q     # 124 passed, no network/LLM/Mireye key required
.venv/Scripts/python.exe scripts/run_pipeline.py  # record -> event -> physical features -> ALERT/SILENCE
```

`run_pipeline.py` ingests a real bill through Phase 1's actual pipeline (classify →
deterministic stage resolution → canonicalize, including a real LLM extraction call —
see below), registers two physically contrasting sites through Phase 2's actual pipeline
(quote → fetch → store → score), and evaluates the same ADOPTED event against both
through Phase 3 — producing an `ALERT` for the physically material site and a `SILENCE`
for the physically irrelevant one, each with full government and physical evidence. See
`context/phase3.md` for the design behind each gate, and the bottom of this file for
real example output.

The test suite is fully offline by design (no network/LLM/Mireye key required, ever —
`context/phase3.md` D7/D8). `run_pipeline.py` always fakes Mireye too, so its cost and
output stay reproducible regardless of live data (D7) — but it *does* run a real LLM
extraction call when a working key is configured: this repo currently has both an
`OPENAI_API_KEY` (primary) and `GEMINI_API_KEY` (automatic fallback) verified working in
`.env`, so `python scripts/run_pipeline.py` as shown above is already exercising real
Phase 1 LLM extraction. Real `MIREYE_API_TOKEN` is also configured and verified working
end to end (D7) but is intentionally not the demo's default path, for the same
reproducibility reason. See "Setup" below for what's needed to run with no credentials
at all, or to point Phase 2 at the live Mireye API.

**Serve the combined API** (Phase 1 mounted at `/phase1`, Phase 2 + Phase 3 as routers),
from `backend/`:
```bash
.venv/Scripts/python.exe scripts/seed_demo.py
.venv/Scripts/python.exe -m uvicorn phase3.app:app --reload --port 8000
```

`seed_demo.py` writes the offline demo into the persistent databases used by the
console. `run_pipeline.py` is an isolated in-memory proof and intentionally leaves the
served databases unchanged.

**Serve the console**, from `frontend/` in a second terminal — it proxies `/api/*` to the
backend above, so start the backend first:
```bash
.venv/Scripts/python.exe -m uvicorn app:app --reload --port 8080
```
Then open <http://127.0.0.1:8080>. The map needs a Google Maps browser key in
`frontend/.env`; without one the console falls back to an inline SVG vicinity diagram, so
the demo survives having no key and no network. See `frontend/README.md`.
- `GET  /phase1/events`, `GET /phase1/events/{id}` — Phase 1
- `POST /v1/sites`, `GET /v1/sites/{id}/{bundle}`, `GET /v1/fetch-log` — Phase 2
- `POST /v1/decide` — Phase 3 (`{event_id or event, site_id}` → `MaterialityDecision`)

---

## Phase 1 subsystem — Public Record / Event Intelligence

Turns Seattle Legistar government records into canonical, deduplicated, stage-tracked,
provenance-backed Events, exposed as clean JSON for Phase 2 and Phase 3 to consume.

```
Seattle Legistar (real API, no auth)
        ↓  sources/seattle_legistar.py  (discover/fetch, filtered to CB/Res/Ord)
Document (raw normalized record, persisted for provenance)
        ↓  classify.py (cheap keyword filter)
relevant?  ── no → skipped, stored, not alerted on
        │ yes
        ↓  stage_resolver.py (deterministic, from MatterHistory/MatterStatus)
        ↓  extract.py (LLM, event_type/title/description/geography;
        │              OpenAI primary / Gemini fallback, see LLM_PROVIDER;
        │              falls back to classify.guess_event_type() heuristic
        │              if neither key works or both calls fail)
        ↓  canonicalize.py (dedup by bill number + stage-transition upsert)
Event (canonical, versioned, with Evidence) + geo.py (geography, defaults
        to UNRESOLVED rather than guessing)
        ↓  ingest.py ties all of the above together
        ↓  api.py
Clean JSON contract for Phase 2 (Mireye) + Phase 3 (monitor/orchestration)
```

## Status: tested end to end

`pytest -q` → **124 passed** across the merged repo (19 from this subsystem plus
Phase 2 and Phase 3's suites), including a full pipeline test
(`tests/test_ingest.py`) that runs the exact scenario from the spec: a
data-center moratorium bill moving PROPOSED → HEARD → ADOPTED across three
separate ingestion passes, using a fake in-memory source (no network/LLM
needed), and asserts it collapses into **one** canonical Event with a
3-entry version history — not three separate events.

Also covered: irrelevant matters get filtered before any LLM call; repeat
ingestion of the same state doesn't create a duplicate or falsely re-trigger
`stage_changed`; stage never regresses on out-of-order documents.

Live-API-only paths (Legistar network calls, real LLM extraction) aren't run
in the test suite — run the scripts below locally to exercise those.

## Setup

```bash
pip install -r requirements.txt
pytest -q                                  # 124 passed, no network needed
python scripts/probe_legistar.py --types   # confirm Legistar type strings (already done)
```

For real LLM extraction (optional — the keyword heuristic works without it). Either key
alone is enough; with both set, `LLM_PROVIDER` picks which is primary (default `openai`
— see `context/phase3.md` D8 for why) and the other is used as an automatic fallback if
the primary's call fails:
```bash
export OPENAI_API_KEY=...    # PowerShell: $env:OPENAI_API_KEY="..."
export GEMINI_API_KEY=...    # PowerShell: $env:GEMINI_API_KEY="..."
export LLM_PROVIDER=openai   # optional; "openai" | "gemini"
```

For real Mireye physical data (optional — `scripts/run_pipeline.py` uses a fake
transport by default even when this is set, for reproducibility; see `context/phase3.md`
D7 — verified working live otherwise):
```bash
export MIREYE_API_TOKEN=...    # PowerShell: $env:MIREYE_API_TOKEN="..."
```

## Running it for real

**One matter, by ID:**
```bash
python scripts/run_ingest.py --matter-id 121279
python scripts/run_ingest.py --matter-id 121279 --no-llm   # heuristic-only, no API key needed
```

**Everything updated in the last N days:**
```bash
python scripts/run_ingest.py --days 14
```

**Historical replay** (chronological, preserves original Legistar dates so
lead-time can be evaluated downstream):
```bash
python scripts/replay.py --start 2026-01-01 --end 2026-07-01
python scripts/replay.py --start 2026-01-01 --end 2026-07-01 --no-llm
```

**Serve the API for Phase 2/3 to consume:**
```bash
uvicorn monitor_records.api:app --reload --port 8000
```
- `GET /health`
- `GET /events` (optional `?stage=` / `?event_type=` filters)
- `GET /events/{event_id}`

## The contract Phase 2 and Phase 3 consume

```json
{
  "event_id": "evt_...",
  "canonical_id": "seattle:seattle_legistar:cb-121214",
  "event_type": "MORATORIUM",
  "stage": "ADOPTED",
  "title": "Temporary moratorium on new data centers",
  "description": "...",
  "jurisdiction": "Seattle",
  "geography": { "type": "JURISDICTION", "name": "Seattle" },
  "introduced_at": "2026-06-01T00:00:00",
  "heard_at": "2026-06-03T00:00:00",
  "adopted_at": "2026-06-09T00:00:00",
  "confidence": 0.9,
  "first_seen_at": "...",
  "last_seen_at": "...",
  "evidence": [
    { "source_url": "...", "document_id": "...", "passage": "...", "reason": "..." }
  ]
}
```

Phase 2 (Mireye) and Phase 3 (monitor/orchestration/frontend) should only
ever need this shape — nothing about Legistar, MatterId, or the scraper
should leak into their code.

## Design decisions worth knowing about

- **Stage is never LLM-authoritative.** `ingest.resolve_stage()` checks
  MatterHistory action rows first, then the Matter's own MatterStatusName,
  and only falls back to the LLM's opinion if neither structured signal is
  present. Verified live against real Seattle data on 2026-08-23 (see
  `stage_resolver.py` maps).
- **Matter type filtering.** Legistar's `/matters` endpoint returns a lot of
  procedural noise (Introduction & Referral Calendars, Minutes, Information
  Items) that can carry misleading `MatterStatusName` values (e.g. an IRC
  showing "Adopted" just means the weekly calendar was adopted, not that a
  law passed). `sources/seattle_legistar.py` restricts `discover()` to
  `Council Bill (CB)`, `Resolution (Res)`, `Ordinance (Ord)` — confirmed
  against live data.
- **No-LLM fallback exists on purpose.** `classify.guess_event_type()` is a
  coarse keyword→EventType heuristic used when `GEMINI_API_KEY` isn't set
  or the call fails, so ingestion never hard-stops on LLM availability. It
  produces lower-confidence events (0.4) and should be treated as a
  placeholder for the real extraction once wired.
- **Geography defaults to UNRESOLVED, never guessed** (`geo.py`) unless the
  LLM extraction supplies real coordinates/a jurisdiction name. No geocoding
  is implemented — MVP is effectively citywide-only unless you add one.
- **Attachment text extraction is best-effort** (`text_extraction.py`, PDF
  only, no OCR). Many Legistar staff reports are scanned images with no text
  layer; extraction returns `None` silently rather than failing ingestion.
  Not yet wired into `ingest.py`'s main path — currently only the matter's
  own title/text feeds extraction, not attachment content. Wire in
  `text_extraction.fetch_and_extract_pdf_text()` per-attachment if richer
  context is worth the added latency/cost for the demo.

## Known gaps / explicitly out of scope for the hackathon MVP

- OCR for scanned attachments.
- Real geocoding (address text → lat/long or polygon).
- Fallback `canonical_id` collision risk when no bill number exists — still
  a hash of title+date-bucket, unreviewed.
- No auth/rate-limiting on `api.py` (fine for a hackathon demo, not for
  anything public).

---

## Phase 3 — example output

`python scripts/run_pipeline.py` ingests one real bill (CB121214, a data-center
moratorium) through Phase 1's actual PROPOSED → HEARD → ADOPTED lifecycle, registers two
physically contrasting sites through Phase 2's actual quote → fetch → store → score
pipeline, and evaluates the **same ADOPTED event** against both. Government evidence
below is real Gemini extraction output (`GEMINI_API_KEY` in `.env`), not simulated.
Full design behind each gate: `context/phase3.md`.

**ALERT case** — SODO industrial site (strong grid, fiber, flat, clear of every
constraint flag) — the moratorium removes real option value here:

```json
{
  "decision": "ALERT",
  "canonical_id": "seattle:demo-legistar:cb-121214",
  "stage": "ADOPTED",
  "reasons": [
    "data_center_optionality=1.00 >= 0.5: site is physically material to this event (adopted moratorium)"
  ],
  "government_evidence": [
    {
      "source_url": "https://seattle.legistar.com/LegislationDetail.aspx?ID=CB121214",
      "passage": "An ordinance imposing a temporary moratorium on new data center development citywide.",
      "reason": "This passage describes a legislative action that restricts development, making it a material event."
    },
    {
      "passage": "On June 9, 2026, the ordinance was passed by a vote of 9-0 and signed into law.",
      "reason": "This explicitly states that the ordinance was passed and signed into law, indicating the 'ADOPTED' stage."
    }
  ],
  "metric": "data_center_optionality",
  "score": 1.0,
  "physical_components": {
    "power":   {"score": 1.0, "weight": 0.4,  "basis": "230 kV at nearest line, substation 3400 m away (extra-high voltage (>=230 kV))"},
    "fiber":   {"score": 1.0, "weight": 0.2,  "basis": "fiber_broadband_available = true"},
    "terrain": {"score": 1.0, "weight": 0.15, "basis": "slope 2.1 deg (flat)"},
    "clear":   {"score": 1.0, "weight": 1.0,  "basis": "no constraint flags on record"}
  },
  "replayed": false
}
```
(6 evidence citations in the real output; trimmed to 2 here for brevity.)

**SILENCE case** — the identical ADOPTED, high-confidence, correctly-scoped event
against a Duwamish-floodplain-shaped site — the moratorium removes nothing, because the
site was never buildable for a data center in the first place:

```json
{
  "decision": "SILENCE",
  "canonical_id": "seattle:demo-legistar:cb-121214",
  "stage": "ADOPTED",
  "reasons": [
    "data_center_optionality=0.01 < 0.5: site's physical profile means this event does not materially change its options (weakest factor: fiber - fiber_broadband_available = false)"
  ],
  "government_evidence": [ "...same citation as above..." ],
  "metric": "data_center_optionality",
  "score": 0.009,
  "physical_components": {
    "power":   {"score": 0.06, "weight": 0.4,  "basis": "12 kV at nearest line, substation 32000 m away (distribution-only voltage (<69 kV))"},
    "fiber":   {"score": 0.05, "weight": 0.2,  "basis": "fiber_broadband_available = false"},
    "terrain": {"score": 1.0,  "weight": 0.15, "basis": "slope 1.0 deg (flat)"},
    "clear":   {"score": 0.05, "weight": 1.0,  "basis": "within_floodplain_polygon: true (flagged)"}
  },
  "replayed": false
}
```

The demo also runs a third case first: the **same bill, as real (no-LLM) ingestion
actually produced it** — confidence 0.4, the heuristic fallback's fixed value for a
keyword-only match — against the physically-strongest site. Result: `SILENCE`,
`"confidence 0.40 is below the 0.6 threshold"`. Same ADOPTED bill, same ideal site —
still no alert, because the extraction confidence never crossed the bar. That's the
conservative-by-design requirement working exactly as intended.
