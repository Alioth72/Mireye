# The Monitor — Public Record / Event Intelligence

Person 1's subsystem: turns Seattle Legistar government records into
canonical, deduplicated, stage-tracked, provenance-backed Events, exposed as
clean JSON for the Mireye layer (Phase 2) and the monitor/orchestration/
frontend layer (Phase 3) to consume.

```
Seattle Legistar (real API, no auth)
        ↓  sources/seattle_legistar.py  (discover/fetch, filtered to CB/Res/Ord)
Document (raw normalized record, persisted for provenance)
        ↓  classify.py (cheap keyword filter)
relevant?  ── no → skipped, stored, not alerted on
        │ yes
        ↓  stage_resolver.py (deterministic, from MatterHistory/MatterStatus)
        ↓  extract.py (LLM, event_type/title/description/geography;
        │              falls back to classify.guess_event_type() heuristic
        │              if no ANTHROPIC_API_KEY or the call fails)
        ↓  canonicalize.py (dedup by bill number + stage-transition upsert)
Event (canonical, versioned, with Evidence) + geo.py (geography, defaults
        to UNRESOLVED rather than guessing)
        ↓  ingest.py ties all of the above together
        ↓  api.py
Clean JSON contract for Phase 2 (Mireye) + Phase 3 (monitor/orchestration)
```

## Status: tested end to end

`pytest -q` → **18 passed**, including a full pipeline test
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
pytest -q                                  # 18 passed, no network needed
python scripts/probe_legistar.py --types   # confirm Legistar type strings (already done)
```

For real LLM extraction (optional — heuristic fallback works without it):
```bash
export ANTHROPIC_API_KEY=sk-...    # PowerShell: $env:ANTHROPIC_API_KEY="sk-..."
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
  coarse keyword→EventType heuristic used when `ANTHROPIC_API_KEY` isn't set
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
