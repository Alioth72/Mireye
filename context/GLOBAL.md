The Monitor — Global Context
Branch: docs-update (off phase3, which holds all implementation) · Last updated: 2026-08-24

How to read this file
This is the whole-system entry point. Read this first; it links to context/phase1.md,
context/phase2.md, and context/phase3.md for the full decision log of each phase (every
decision below has a "Decision → Alternatives rejected → Rationale → Consequence →
Reversal cost" writeup over there — this file only summarizes and cross-references).

What this system is
An agentic monitoring system that watches public government records for a location,
detects material events, uses Mireye to understand the physical characteristics of the
affected location, and combines both to decide whether the event materially matters.
Conservative by design: a false ALERT is worse than staying SILENCE.

    public government record
            |  Phase 1 (monitor_records/)
            v
    structured Event (event_type, stage, subject, geography, confidence, evidence)
            |  Phase 3 (phase3/) combines this with:
            v
    Phase 2 physical datapoints (phase2/) -- tri-state, provenance-tagged
            |
            v
    MaterialityDecision: ALERT or SILENCE, with government + physical evidence

Three phases, three questions, three docs
- Phase 1 (context/phase1.md) -- "What happened in the public record, and how do we know?"
- Phase 2 (context/phase2.md) -- "What is physically true at this coordinate, and how do we know?"
- Phase 3 (context/phase3.md) -- "Does that event matter at that location, and how do we know?"
  Phase 3 is the only component holding both halves at once.

Where the code actually lives (all on branch `phase3`, this doc's branch built off it)
- `monitor_records/` -- Phase 1. Legistar adapter, classify, extract (LLM), stage_resolver
  (deterministic), canonicalize (dedup+upsert), api.py.
- `phase2/` -- Phase 2. bundles.py, store.py (tri-state + TTL), mireye/client.py (real
  API client), scoring.py (derived optionality), orchestrator.py (quote->fetch->store->log),
  router.py (HTTP surface).
- `phase3/` -- Phase 3. stage_policy.py, geography.py, bundle_map.py, pipeline.py
  (decide()), models.py (P3Decision, dedup), router.py (POST /v1/decide), app.py
  (the combined FastAPI process).
- `tests/fakes/mireye_fake.py` -- hand-authored fake Mireye backend, two physically
  contrasting coordinate profiles, wired in via httpx.MockTransport.
- `scripts/run_pipeline.py` -- the end-to-end demo. `python scripts/run_pipeline.py`.

How the three phases actually merged (git history, not just docs)
`phase1` and `phase2` were built as sibling branches off a shared empty base (`main` @
80b8bf8), each owning a disjoint file tree. `phase3` was created fresh off `main`,
merged both in (`--no-ff`, one real conflict in `requirements.txt` since both branches
independently created it), then had the actual decision layer built on top -- Phase 2's
own branch had designed but never wired its site-registry/orchestrator/bundle-endpoint
layer (steps 4-6, 8-9 of its own progress table), so that got built during the merge
too, not just Phase 3's new code. Full mechanics: context/phase3.md "Merge notes".

The single most important cross-cutting fact: two bugs were found by actually running
the system, not by reasoning about it
1. Phase 1's own doc (context/phase1.md D9) claimed `subject` propagation was "Fixed" --
   it wasn't. The `Event` model had no column for it. This was caught and actually fixed
   during the merge, not before. Lesson: a "Fixed" entry in a decision log describes
   intent at the time it was written, not a passing test -- verify before trusting it.
2. `scripts/run_pipeline.py` called `session.refresh()` on an object with unflushed
   pending changes, which silently discarded a real, correct confidence/subject update
   from a live LLM call. Real SQLAlchemy behavior, not a logic bug in the pipeline
   itself. Full writeup: context/phase3.md D7.
Neither would have been caught by reading the code -- both only showed up when the
demo was actually run end to end against real APIs.

Conservative-by-design mechanics (why a false ALERT can't easily happen)
- Stage gate: only ADOPTED/REJECTED are alert-eligible. PROPOSED/HEARD/WITHDRAWN/TABLED
  always resolve SILENCE, before any physical evaluation even runs. (phase3/stage_policy.py)
- Confidence floor: MIN_CONFIDENCE=0.6. The heuristic (no-LLM) fallback path always
  produces confidence 0.4 -- this floor specifically and deliberately keeps every
  keyword-only match out of ALERT. (phase3/stage_policy.py, context/phase1.md D5)
- Geography gate: UNRESOLVED geography always -> SILENCE, never guessed. JURISDICTION
  requires an exact name match against the site's boundaries data. POINT requires being
  within 1.5km (unvalidated placeholder, see phase3.md R1). (phase3/geography.py)
- Physical gate: even an ADOPTED, high-confidence, correctly-scoped event goes SILENCE
  if the site's own physical profile means the event doesn't change its real options
  (e.g. a data-center moratorium at a site that was never buildable for one anyway).
  Multiplicative composite scoring (phase2/scoring.py) -- one bad component crushes the
  whole score, an additive model would not.
- Dedup: (canonical_id, stage, confidence_bucket, site_id) is the decision cache key,
  checked before every gate (including stage-gate SILENCEs) so repeat/retried calls
  replay instead of re-deciding. One government action -> one alert, provably.

Credentials -- what's real, what's faked, and why (full detail: phase3.md D7/D8)
- Mireye: real MIREYE_API_TOKEN in .env, verified working end to end against the live
  API (quote/fetch/store/orchestrate, not just the raw client). Demo/tests still
  default to a fake transport on purpose, so runs stay free and reproducible.
- LLM extraction: real OpenAI (primary) and Gemini (automatic fallback) keys in .env,
  both verified. OpenAI is primary because the configured Gemini key is free-tier
  (20 requests/day) and was exhausted by ordinary testing -- a real operational
  constraint discovered in practice, not a preference.
- Legistar: not reachable from this environment (confirmed, see
  scripts/probe_legistar.py's own header). The demo uses canned records for one real
  bill (CB121214) via a FakeSource-style adapter, matching Phase 1's own test pattern.

Open, not-yet-closed risks worth knowing about (see each phase doc's Risks section for
the full list -- this is only the cross-cutting ones)
- context/phase2.md's "Seattle may not discriminate physically" risk is STILL open for
  real data -- only answered for the hand-authored fake demo profiles, not against a
  real 8-coordinate Seattle spread test (~150 credits, never run).
- context/phase3.md R1: POINT_RADIUS_KM (1.5) and ALERT_THRESHOLD (0.5) are both
  unvalidated placeholders, not tuned against real event/coordinate data.
- context/phase1.md R5/R4: LLM confidence is not calibrated (a 0.9 vs 0.95 shouldn't be
  read as meaningful), and was additionally non-deterministic before temperature=0 was
  set -- fixed, but the calibration caveat itself still stands.

How to run
    pip install -r requirements.txt
    pytest -q                          # 124 tests, fully offline
    python scripts/run_pipeline.py     # the end-to-end ALERT/SILENCE demo
    uvicorn phase3.app:app --reload --port 8000   # combined API, all three phases

Full run commands and example output: README.md.
