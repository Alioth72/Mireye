Phase 1 — Public Record / Event Intelligence Branch: phase1 · Owner: Person 1 Status: pipeline built end-to-end, 19 tests passing, live-verified against real Seattle Legistar data Last updated: 2026-08-23

How to read this file This is the integration contract for Phase 1, written for Person 2 (Mireye/physical) and Person 3 (decision layer), in the same format as Phase 2's doc. Section 3 records decisions with what was rejected and why. Section 8 responds directly to the three cross-phase asks Phase 2 raised against Phase 1 — two are resolved, one needs a decision from Phase 3.

Section 3 — design decisions, with alternatives and reversal cost Section 4 — data model Section 5 — API surface Section 6 — event taxonomy / stage vocabulary Section 7 — contracts (what Phase 1 gives you, what it will not) Section 8 — responses to Phase 2's open asks Section 9 — risks Section 10 — progress Section 11 — configuration

Position in the system Phase 1 ── what happened in the public record (events) <-- this file Phase 2 ── what is physically true at this location (datapoints) Phase 3 ── does that event matter at that location (decision)

Phase 1 answers exactly one question:

What happened in the government's public record, and how do we know?

It never sees a site, a coordinate, a datapoint, or a physical field. It has no concept of "does this matter" — that judgment belongs entirely to Phase 3, same boundary principle as Phase 2's D1.

Why this phase exists One government event produces many documents — an agenda, a staff report, a hearing record, minutes, an amendment, a final vote. A system that alerts once per document is a keyword feed with extra steps. Phase 1's job is to collapse that document stream into canonical, deduplicated, stage-tracked Events, so Phase 3 evaluates each real legislative event exactly once per meaningful state change — not once per paper.
Design decisions Format: Decision → Alternatives rejected → Rationale → Consequence → Reversal cost.

D1 — Stage is never LLM-authoritative Decision. stage_resolver.py derives PROPOSED/HEARD/ADOPTED/... purely from Legistar's own structured fields (MatterHistory action rows, MatterStatusName). ingest.resolve_stage() checks these first; the LLM's opinion is only used when neither structured signal is present.

Alternatives rejected. Trusting the LLM extraction's stage field directly, since it's simpler to wire (one call, one field).

Rationale. Legal/status state has to be right, and Legistar already tells us the truth structurally — a MatterHistory row with passed_flag: true is a recorded vote, not an inference. Using the LLM here would mean the one field that most needs to be correct is the one field resting on a hallucination risk.

Consequence. A bill can only reach ADOPTED in this system when Legistar's own record says so. The LLM's stage output is effectively a fallback for the rare case where structured data is missing.

Reversal cost. High, and it should not be reversed — this is the same class of guard as Phase 2's D11 (multiplicative scoring). If an LLM-derived stage ever overrides a structured one, treat it as a regression.

D2 — canonical_id from bill number, with an explicitly weaker fallback Decision. canonical_event_id(jurisdiction, source, external_legislation_id) — when Legistar's MatterFile (e.g. "CB 121214") is present, dedup is exact and stable. When it's absent, falls back to a hash of normalized title + date bucket.

Alternatives rejected. Deduplicating on LLM-generated titles alone.

Rationale. Bill numbers are the one thing guaranteed stable across every document belonging to the same legislation. Titles drift between an agenda item and a staff report describing the same bill; hashing a title would silently create duplicate events.

Consequence. Events created via the fallback path are structurally weaker — more collision-prone — and are worth treating as lower-confidence downstream (see R1).

Reversal cost. Low mechanically, but the primary path (bill number) should never be bypassed when available.

D3 — Legistar matter-type filtering happens at the source, before classify.py even runs Decision. discover() in sources/seattle_legistar.py restricts results to Council Bill (CB), Resolution (Res), Ordinance (Ord).

Alternatives rejected. Pulling every Matter type and relying on the keyword filter alone.

Rationale. Confirmed against live Seattle data: Legistar's /matters endpoint returns a lot of procedural noise (Introduction & Referral Calendars, Minutes, Information Items) that can carry misleading MatterStatusName values — an IRC showing "Adopted" means the weekly calendar was adopted, not that a law passed. Filtering at the source avoids burning LLM calls on non-legislation and avoids a status-string collision with D1's trust model.

Consequence. Appointments and Clerk Files are also excluded; if a future need requires tracking one of those types, this list needs to grow deliberately, not implicitly.

Reversal cost. Low — it's a set literal in one file.

D4 — Stage only ever moves forward Decision. STAGE_ORDER in models.py gives each stage a rank; canonicalize.upsert_event() only lets Event.stage advance, never regress, when a new document arrives.

Alternatives rejected. Always trusting the latest document's stage, whatever it says.

Rationale. Documents don't always arrive in chronological order (a stale record can be reprocessed, a replay can hit records out of sequence). An event that's already ADOPTED must not un-adopt because an "introduced" record was seen late.

Consequence. Every document is still recorded as an EventVersion row for provenance/replay even when it doesn't move Event.stage — so the history is complete even though the canonical state only progresses.

Reversal cost. Not reversible without accepting incorrect stage flapping.

D5 — A no-LLM heuristic fallback exists on purpose Decision. classify.guess_event_type() is a coarse keyword→EventType mapping used whenever the LLM call is unavailable or fails; it produces a deliberately low confidence (0.4) event rather than skipping ingestion entirely.

Alternatives rejected. Hard-failing ingestion when no LLM key is configured.

Rationale. Development and demo continuity shouldn't depend on an API key being present at every moment. The heuristic is coarse on purpose (e.g. every permit mention lands in MAJOR_DEVELOPMENT_PERMIT regardless of scale) — it's a bridge, not a substitute.

Consequence. Events created this way have confidence: 0.4 and no subject, description, or evidence — Phase 3 should treat that confidence value as a real signal to route to review rather than auto-alert.

Reversal cost. None — it's inert once a working LLM key is always present.

D6 — Geography defaults to UNRESOLVED, never guessed Decision. geo.py only sets POINT or POLYGON when the LLM extraction supplies real coordinates/geometry; otherwise geography is JURISDICTION (citywide) or UNRESOLVED.

Alternatives rejected. Attempting to geocode neighborhood/address mentions found in free text.

Rationale. No geocoder is wired in for MVP. Hallucinated geometry is worse than an honest "we don't know" — same principle as Phase 2's D7/D9 refusal to let a failed fetch present as a value.

Consequence. This system is effectively citywide-only in practice today. Phase 2's D13 (boundaries bundle, JURISDICTION-scope) already anticipated this — the majority of events from a Legistar feed will be JURISDICTION-scope, which is the case this pairing was built for.

Reversal cost. None, additive — a geocoder can be added later without changing the schema.

D7 — LLM provider is isolated behind one function Decision. extract.call_llm_extract() is the only place that knows which LLM provider is in use. ingest.py calls it and only depends on the ExtractedEvent contract, never on provider specifics.

Alternatives rejected. Baking Anthropic (or any provider)'s SDK calls directly into ingest.py.

Rationale. This was tested in practice, not just designed: the project was built against Claude, then switched to Gemini mid-build. The switch touched exactly one function.

Consequence. Provider-specific quirks live in one place — e.g. Gemini's response_schema currently has to be hand-built rather than auto-converted from the Pydantic model, because the (deprecated) google-generativeai SDK chokes on Pydantic's "default" keys in the generated JSON schema. That workaround (GEMINI_RESPONSE_SCHEMA in extract.py) is isolated to this one function too.

Reversal cost. None — this is the seam by design.

D8 — Attachment text extraction is best-effort and not yet wired into the main path Decision. text_extraction.py does best-effort PDF text extraction (returns None silently on scanned/failed PDFs). It exists but ingest.py's main path currently only feeds the Matter's own title/text to the LLM — not attachment content.

Alternatives rejected. Wiring attachment extraction into every ingest pass immediately.

Rationale. Many Legistar staff reports are scanned images with no text layer; OCR is out of scope for the hackathon MVP. Wiring in an extraction step that mostly returns None adds latency without proportionate value until verified against real samples.

Consequence. Extraction currently works only from the bill's title/short description. Substantive content that lives only in a staff report attachment is invisible to the LLM today (see R3).

Reversal cost. Low — the function exists; wiring it in per-attachment is additive.

D9 — subject propagates end-to-end Decision. ExtractedEvent.subject (e.g. "data centers" vs. "BESS") is stored on Event and exposed in the API output.

Alternatives rejected. None — this was a real bug, not a design choice: subject was captured by extraction but silently dropped before reaching the API. Fixed after Phase 2 flagged it (see section 8).

Rationale. Phase 3's bundle-selection table (Phase 2 §6.2) branches on subject — a data-center moratorium and a BESS moratorium check different physical fields even though both are MORATORIUM-type events. Without subject, Phase 3 can't tell them apart.

Consequence. The extraction prompt now explicitly asks for a specific subject rather than a vague phrase.

Reversal cost. None; this is a completed fix, not an open decision.

Data model SQLAlchemy, SQLite by default (MONITOR_DB_URL env var to point elsewhere).

Document id, source, source_url, external_id, document_type title, published_at, meeting_date content_hash, raw_text, doc_metadata (json) created_at

Event id, canonical_id (unique, indexed) -- dedup key, D2 event_type, title, description, subject -- subject: D9 jurisdiction, stage geography_type, geography (json) -- D6 introduced_at, heard_at, adopted_at, effective_at first_seen_at, last_seen_at, confidence

EventVersion -- append-only, D4 id, event_id, document_id, stage, occurred_at, created_at

Evidence id, event_id, document_id, passage, section, url, reason, created_at

API surface Implemented GET /health GET /events optional ?stage= / ?event_type= filters GET /events/{event_id}

Not implemented (not needed yet, flag if Phase 3 wants them) No webhook/push — Phase 3 polls or is called by orchestration on its own schedule No filter by jurisdiction (single-jurisdiction MVP, moot for now) No pagination (fine at hackathon data volumes)

Event JSON shape: { "event_id": "evt_...", "canonical_id": "seattle:seattle_legistar:cb-121214", "event_type": "MORATORIUM", "stage": "ADOPTED", "title": "...", "subject": "data centers", "description": "...", "jurisdiction": "Seattle", "geography": { "type": "JURISDICTION", "name": "Seattle" }, "introduced_at": "...", "heard_at": "...", "adopted_at": "...", "confidence": 0.9, "first_seen_at": "...", "last_seen_at": "...", "evidence": [ { "source_url": "...", "document_id": "...", "passage": "...", "reason": "..." } ] }

Event taxonomy / stage vocabulary event_type: REZONING, ANNEXATION, COMP_PLAN_AMENDMENT, UTILITY_EXTENSION, MORATORIUM, MAJOR_DEVELOPMENT_PERMIT

stage (six values — see section 8 for the mismatch with Phase 3's four): PROPOSED, HEARD, ADOPTED, REJECTED, WITHDRAWN, TABLED

Contracts 7.1 What Phase 1 gives you A canonical, deduplicated Event per real piece of legislation — not per document Deterministic stage (D1) wherever Legistar's own record supports it subject, for bundle selection (D9) Evidence with source_url + passage for every event that has LLM-derived fields geography marked UNRESOLVED rather than guessed when we don't know (D6)

7.2 What Phase 1 will not give you A materiality judgment, or anything about physical fitness of the site — that's Phase 2/3 Site coordinates or any concept of "location" beyond jurisdiction/geometry-if-known A guarantee that fallback-path canonical_ids (D2) never collide — treat those as lower trust Real-time push — Phase 1 is pull-only today (GET /events)

Responses to Phase 2's open asks (their doc, section 8) "Keep subject in the emitted event" — Fixed (D9). Was a real bug: captured in extraction, dropped before the interface object. Now flows through end to end. "Reconcile stage vocabulary" — Not resolved by Phase 1 alone; needs Phase 3's input. Phase 1 emits six stages (PROPOSED HEARD ADOPTED REJECTED WITHDRAWN TABLED) and has no plan to reduce that set — REJECTED and WITHDRAWN/TABLED are semantically different outcomes (a rejected moratorium is a legitimate positive-direction event; a withdrawn one is closer to a non-event), and collapsing them upstream would destroy information Phase 3 might want. Proposal: Phase 1 keeps all six; Phase 3's mapping to its internal four-value vocabulary is Phase 3's judgment call to make and revise, same as Phase 2 kept scoring-band judgment on their side (D10) rather than pushing it upstream. If Phase 3 would rather Phase 1 do the collapsing, say which of the six map to which of the four and it's a one-line change in api.py. "Agree the confidence type" — Phase 1 emits confidence as a float in [0, 1] (either the LLM's self-reported confidence, or a flat 0.4 for heuristic-only events — D5) and does not plan to convert it to a string bucket. Same reasoning as the stage question: bucketing thresholds (what counts as "high") are a downstream judgment, not something Phase 1 should decide unilaterally. Phase 2 already normalizes Mireye's own high|medium|low|unknown on their side; whoever owns the Phase 1 float → Phase 3's expected string still needs to be named — this doc is that ask, restated back: if Phase 3 wants Phase 1 to do the mapping instead, provide the thresholds and it's a one-line change.
Risks R1 — Fallback canonical_id collision risk (D2). Unreviewed in practice; only exercised in tests, not against a real Legistar record lacking a bill number. Worth a real check before demo day.

R2 — Matter-type filter (D3) is a manually maintained set confirmed against one snapshot of live data (2026-08-23). If Seattle's Legistar instance uses a type string not yet seen (e.g. a rare "Contract" or "Proclamation" type carrying real legislative content), it's silently excluded. Re-run scripts/probe_legistar.py --types periodically.

R3 — Substantive content in attachments (staff reports) is not yet fed to extraction (D8). A bill whose title is generic but whose staff report contains the real rezoning detail may be under-classified today.

R4 — Gemini structured-output fragility. The hand-written GEMINI_RESPONSE_SCHEMA (D7) exists because the SDK in use (google-generativeai, now deprecated in favor of google-genai) doesn't cleanly auto-convert Pydantic schemas. If Google fully sunsets the old SDK, this breaks — worth migrating to google-genai before it's forced.

R5 — confidence is not calibrated. LLM self-reported confidence and the flat 0.4 heuristic value are not validated against any ground truth. Phase 3 should not read a difference between e.g. 0.9 and 0.95 as meaningful yet.

R6 — scripts/replay.py has no persistence of "already replayed this range" — rerunning the same historical window redoes the same LLM calls. Cheap at hackathon scale, worth a guard before a large-range replay.

Progress Step State 1 Legistar adapter (discover/fetch) done, live-verified 2 Data model (Document/Event/Version/Evidence) done 3 Deterministic stage resolution done, live-verified against real MatterStatusName/history strings 4 Document classification (keyword filter) done 5 LLM extraction done, live-verified (Gemini) 6 Canonicalization / dedup done, tested (5 docs -> 1 event) 7 Stage version history done 8 Geography (JURISDICTION/POINT/POLYGON) done — POINT/POLYGON require LLM-supplied coords, no geocoder 9 Historical replay script built (scripts/replay.py), not yet run over a full 6-12mo window 10 Clean interface (api.py) done, subject fixed (D9)

Not done: attachment text extraction wired into main path (D8), OCR, real geocoding, replay idempotency (R6).

Test suite: 19 tests, all passing, no network/LLM key required. Run: pytest -q. Run locally: uvicorn monitor_records.api:app --reload --port 8000

Configuration Variable Default Effect GEMINI_API_KEY — required for real LLM extraction; falls back to heuristic if unset (D5) MONITOR_DB_URL sqlite:///./monitor_records.db point at Postgres etc. by changing this