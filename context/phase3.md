Phase 3 — Materiality Decision Layer
Branch: phase3 (merged from phase1 + phase2, off main @ 80b8bf8) · Owner: integration
Status: merge complete, decision pipeline built and tested end-to-end, 124 tests passing.
Both Mireye and LLM (OpenAI + Gemini) real credentials verified working live.

How to read this file
This is the integration contract for Phase 3, in the same format as Phase 1's and
Phase 2's docs. Phase 3 is the only component that holds both halves of the system at
once — it consumes Phase 1's Event contract and Phase 2's physical-datapoint contract
and decides ALERT or SILENCE. Section "Responses to open asks" closes out every
cross-phase question Phase 1 and Phase 2 raised against Phase 3 in their own docs.

Position in the system
Phase 1 -- what happened in the public record (events)
Phase 2 -- what is physically true at this location (datapoints)
Phase 3 -- does that event matter at that location (decision)   <-- this file

Phase 3 answers exactly one question: does this event materially matter at this
monitored site, and how do we know -- citing both the government evidence and the
physical evidence that produced the answer.

Why this phase is conservative by design
A false ALERT is worse than a missed one. The whole point of holding physical fact
alongside government record is to be able to say "we stayed quiet on non-buildable
ground" -- a keyword feed can't do that. Every gate in the pipeline is built to prefer
`uncertain -> SILENCE` over `uncertain -> ALERT`.

Merge notes
`git checkout -b phase3 main; git merge --no-ff phase1; git merge --no-ff phase2`. One
real conflict: `requirements.txt` (added independently on both branches from the same
empty base) -- resolved to the union, the higher pin where both specified a package,
`anthropic` dropped (dead -- extract.py imports `google.generativeai`, not `anthropic`;
nothing in the codebase imports `anthropic`), `google-generativeai` and `python-dotenv`
added (both were imported by existing code but never listed on either branch). No other
conflicts -- `monitor_records/` and `phase2/` are disjoint trees, and `context/phase2.md`
was already identical on both branches.

Pre-existing gap fixed as a prerequisite, not a nice-to-have: `subject` was documented
in Phase 1's own context doc (D9) as "fixed, flows through end to end" but was never
actually wired past `ExtractedEvent` -- `Event` had no `subject` column, `upsert_event()`
didn't accept it, `ingest.py` never passed it, `api.py` never exported it. Phase 3's
bundle selection depends on `subject` existing on the wire, so this was fixed first
(monitor_records/models.py, canonicalize.py, ingest.py, api.py -- ~15 lines, 4 files) and
covered by tests/test_canonicalize.py and tests/test_api.py.

Also fixed: scripts/run_ingest.py and scripts/replay.py could not actually import
`monitor_records` when run as documented (`python scripts/run_ingest.py` from repo
root) -- Python only puts the script's own directory on sys.path, not the cwd. Both now
insert the repo root explicitly. `scripts/run_ingest.py`'s docstring and requirements.txt
also still said `ANTHROPIC_API_KEY` even though the code has used `GEMINI_API_KEY`
since D7 -- corrected.

Design decisions

D1 -- Stage-eligibility gate, not stage collapsing (closes Phase 1's open ask)
Decision. Phase 3 keeps all six of Phase 1's stages verbatim. `ALERT_ELIGIBLE_STAGES =
{ADOPTED, REJECTED}` (phase3/stage_policy.py) -- only a decided, terminal outcome is
worth a physical evaluation. PROPOSED/HEARD are not yet decided; WITHDRAWN/TABLED are
non-events for materiality purposes. REJECTED is included deliberately: Phase 1's own
reasoning is that a rejected moratorium restores option value at a site, which is a
legitimate positive-direction alert, not a non-event.
Alternatives rejected. Collapsing to Phase 3's originally-envisioned four-value
vocabulary (proposed/heard/adopted/unknown), which is what Phase 1's doc flagged as
information-destroying for REJECTED specifically.
Rationale. Phase 1 explicitly asked Phase 3 to own this judgement rather than push
collapsing logic upstream, matching the same boundary principle Phase 2 used for its
own scoring-band judgement (D10, kept on their side).
Reversal cost. Low -- it's one frozenset.

D2 -- confidence float -> bucket mapping (closes Phase 1's open ask)
Decision. `confidence_bucket()` (phase3/stage_policy.py): <0.6 "low", <0.85 "medium",
else "high". `MIN_CONFIDENCE = 0.6` is the alert-eligibility floor -- below it, an
event is treated as too uncertain to act on regardless of stage. This is not an
arbitrary number: 0.4 is the exact, fixed value `ingest.py`'s heuristic fallback
(`classify.guess_event_type`, no LLM) hardcodes for every event it produces, so 0.6
specifically and deliberately excludes every keyword-only match from ever reaching
ALERT.
Rationale. Phase 1 didn't want to pick a threshold unilaterally (same reasoning as their
stage-vocabulary ask); Phase 3 is the consumer that has to act on the number, so Phase 3
owns where the line is.
Consequence. The bucket is part of the dedup key (D5) precisely so a later, materially
more confident re-extraction of the same (canonical_id, stage) produces a fresh decision
instead of replaying a stale one.

D3 -- geography gate: never guess relevance
Decision. `geography_gate()` (phase3/geography.py). JURISDICTION -> exact case-
insensitive match against the site's `political_locality`/`political_region` (Phase
2's boundaries bundle, pulled once at registration). POINT -> haversine distance
against `POINT_RADIUS_KM = 1.5`. UNRESOLVED -> always SILENCE. POLYGON -> not
implemented in v1 (Phase 1 never emits it in practice -- no geocoder wired), treated
conservatively as unresolved rather than attempting point-in-polygon.
Rationale. Same principle Phase 1 applies in its own geo.py (default to UNRESOLVED
rather than hallucinate geometry) and Phase 2 applies in store.py (never let a failed
fetch present as an answer): an honest "can't confirm" beats a guessed match.
Risk. `POINT_RADIUS_KM = 1.5` is an unvalidated placeholder, same caveat Phase 1 gives
its own fallback canonical_id collision risk (R1) -- tune against real coordinate data
before treating it as load-bearing. Low practical exposure today since Phase 1's
geo.py only emits POINT when the LLM extraction supplies real coordinates.

D4 -- bundle selection is a keyword classifier over free text, with a safe-superset
fallback, never a literal (event_type, subject) table lookup
Decision. `bundle_map.bundles_for()`. Phase 2's recommendation table
(context/phase2.md) is keyed by human categories like "data center" vs "BESS", but
`subject` is LLM-written free text with no enum -- there is no guarantee it matches a
fixed vocabulary, and the heuristic fallback path never populates it at all (subject is
`None`). When ambiguous or unrecognized, `bundles_for()` always returns the wider,
more expensive bundle set, never the narrower one.
Rationale. Guessing toward the cheaper bundle risks silently missing the one field that
would have made an event material (e.g. dropping `telecom` for what was actually a
data-center moratorium). Credits are not scarce (Phase 2's own decision); a wrong
SILENCE from an under-fetched bundle is the expensive failure, not a few extra credits.
Reversal cost. Low -- pure function, easily extended with more keywords.

D5 -- dedup key includes the confidence bucket, and the dedup check runs before every
gate, not just before the physical-evaluation step
Decision. `P3Decision` is keyed on `(canonical_id, stage, confidence_bucket, site_id)`
(phase3/models.py), and `pipeline.decide()` checks this cache FIRST, before the stage
gate. Confidence in the key (see D2) means a later, more-confident re-extraction of an
unchanged stage produces a new decision rather than replaying a stale one.
Alternatives rejected (and actually shipped, then reverted after a real bug). An
earlier version ran the dedup check only after the stage gate, on the reasoning that a
stage-gated SILENCE was "free" and didn't need protecting. This was wrong: a
stage-gated SILENCE is still persisted (for audit-trail completeness), so a second call
for the same key tried to INSERT the same row twice and hit the UNIQUE constraint --
caught by scripts/run_pipeline.py's own demo run, not by the original test suite (the
suite's dedup test only exercised the physically-evaluated path). Fixed, and a
regression test added (tests/test_pipeline.py::test_repeat_call_on_a_stage_gated_silence_does_not_crash_or_duplicate).
Consequence. "One government action -> one alert" holds even under retries or
at-least-once delivery from whatever calls POST /v1/decide, for every gate outcome, not
just the physically-evaluated ones.

D6 -- physical materiality lives in phase2/scoring.py, not phase3/
Decision. Derived optionality scores (`data_center_optionality`, `bess_optionality`,
`buildability`) are computed in Phase 2's package, following the exact formula in
Plan/phase2-mireye-backend-ideation.md sec. 5.4 (voltage-banded power, binary fiber
absent entirely from bess_optionality, slope-banded terrain, worst-flag-wins clear
component) -- multiplicative composition, never additive, so a site with no power scores
near-zero no matter how flat or clear it is. Phase 3 calls `phase2.scoring.score_metric()`
in-process and applies only the ALERT_THRESHOLD (0.5) and the government-side gates.
Rationale. `context/phase2.md`'s "Decisions taken" table already commits to "Phase 2
computes defaults; Phase 3 may override weights/thresholds" -- building this logic
directly in phase3/ instead would have quietly violated that division of labor.
Deferred, not cut. `GET /v1/sites/{id}/derived/{metric}` (the HTTP route) was not built
-- nothing needs it over the wire post-merge, Phase 3 calls the function directly. The
contract is honored in-process; only the HTTP surface is deferred.
The single highest-leverage correctness detail in this file: `absent` on a constraint
field (e.g. `intersects_wetland`) is treated as confirmed clearance (full score) per
Phase 2's own cross-phase ask ("`absent` is not missing... it RAISES optionality");
`ok`+true is the real penalty; `failed`/missing is a distinct, mild uncertainty penalty,
never silently collapsed into either of the other two. Covered by
tests/test_scoring.py's three-way status tests.

D7 -- Mireye is faked at the HTTP boundary for the demo/tests by choice, not by necessity
Decision. tests/fakes/mireye_fake.py is an `httpx.MockTransport` wired into the real
`MireyeClient` (which already supported dependency-injecting a transport). Every real
code path -- quote, fetch, store, orchestrate, score -- runs for real; only the actual
network call to api.mireye.com is faked. Two coordinate profiles (GOOD_SITE, BAD_SITE)
are hand-authored to be physically discriminating on purpose -- this directly answers
the risk phase2.md itself flagged ("Seattle may not discriminate physically"): the
demo's SILENCE case is a real, motivated outcome of the scoring math (0.009 vs 1.0 on
data_center_optionality), not a coin flip.

A real `MIREYE_API_TOKEN` was later provided and verified working end to end against
the live API: `meta_fields`/`usage`/`quote` (free), plus two small real fetches (terrain,
3 credits; boundaries, 4 credits) through the actual `phase2.orchestrator.fetch_and_store`
+ `phase2.store` code path -- not just the raw client. Real response validated correctly
against the branch's existing Pydantic schemas with no changes needed, and the real
boundaries data for GOOD_SITE's coordinates (political_locality=Seattle,
political_county=King County) matches what mireye_fake.py's hand-authored profile
assumed. Despite this, `scripts/run_pipeline.py` and the automated test suite still use
the fake transport by default -- switching them to the live API would make every test
run and every demo run spend real, non-reproducible credits (the account had 25,000
included/month, 5,209 remaining at time of writing) and would make CI/grading outcomes
depend on live data that can drift. The live path is proven to work; it's just not the
default path. `MIREYE_BASE_URL`/`MIREYE_API_TOKEN` in `.env` are enough to point
`phase2.mireye.client.MireyeClient()` (no transport override) at the real API directly.

Phase 1's LLM leg is NOT faked either, and both a Gemini and an OpenAI key turned out to
be available (Legistar itself is still not reachable from this environment -- confirmed
both ways, see scripts/probe_legistar.py's own header and a direct connectivity check).
`scripts/run_pipeline.py` runs a real `call_llm_extract()` call as its "Scenario B" -- not
a hand-simulated confidence bump. Three real issues surfaced by actually exercising this
path, all fixed -- see D8 for the provider one:
- `extract.py`'s Gemini call had no `temperature` set, so the self-reported `confidence`
  (and in principle any field) could vary between two calls on the identical prompt --
  not just uncalibrated (R5) but unstable run-to-run. Fixed: `temperature=0.0` (both
  providers now).
- The demo script called `session.refresh(event_row)` after an update that was never
  flushed or committed -- `refresh()` discards unflushed pending changes and reloads the
  last-persisted state, silently reverting the just-computed confidence/subject update.
  This is a real, general SQLAlchemy behavior worth knowing, not specific to this
  script -- `session.commit()` at the natural transaction boundary (matching how the
  real `monitor_records.db.get_session()` context manager already auto-commits) is the
  fix, not `refresh()`.

D8 -- two LLM providers, OpenAI primary, automatic cross-provider fallback
Decision. `extract.py` now wires both Gemini (`_call_gemini_extract`, original) and
OpenAI (`_call_openai_extract`, new -- `chat.completions.parse()` with `ExtractedEvent`
passed directly as `response_format`, so the SDK derives the schema and returns an
already-validated instance with no manual JSON parsing, unlike the Gemini path whose
older SDK needed a hand-built schema). Both share the exact same `SYSTEM_PROMPT`/
`USER_PROMPT_TEMPLATE` so extraction *behavior* can't drift between providers -- only the
API call differs, per D7 in context/phase1.md ("LLM provider isolated behind one
function"). `call_llm_extract()` picks a provider (env var `LLM_PROVIDER`, else
auto-detected by which key is present, defaulting to OpenAI) and, if that call fails AND
the other provider's key is also configured, automatically retries with the other
provider before giving up -- only then does it raise, which `ingest_matter()`'s existing
try/except catches and falls back to the keyword heuristic.
Alternatives rejected. Defaulting to Gemini (the original provider) when both keys are
present.
Rationale. This is not a preference call -- the configured `GEMINI_API_KEY` is on the
free tier (20 requests/day) and was exhausted by ordinary testing of this exact code
path during development (a real `429 ResourceExhausted` from `generativelanguage.
googleapis.com`, not a hypothetical). OpenAI has no such ceiling on the configured
account. The auto-fallback direction (OpenAI primary -> Gemini fallback) exists
specifically so a transient primary-provider failure (rate limit, outage) doesn't force
every affected document all the way down to the low-confidence heuristic path when a
second working LLM is one call away -- that matters because the heuristic path is
exactly what MIN_CONFIDENCE=0.6 (D2) is built to keep out of ALERT, so an unnecessary
heuristic fallback isn't just lower quality, it can silently suppress a real alert.
Consequence. An explicit `LLM_PROVIDER` pin is honored exactly and never auto-overridden
by availability -- auto-fallback only activates when the caller didn't pin anything,
so deliberately testing/comparing one provider still works as expected.
Reversal cost. Low -- `LLM_PROVIDER=gemini` in `.env` reverts the default with no code
change. Tested offline via mocking at the `_call_gemini_extract`/`_call_openai_extract`
boundary (tests/test_extract_provider.py) rather than hitting either real API, so this
logic is covered without competing for Gemini's daily quota.

Data model
Phase 3 gets its own SQLite file (`phase3.db`, `PHASE3_DATABASE_URL` to override),
separate from `monitor_records.db` and `phase2.db`. No cross-DB joins are needed --
Phase 3 composes an Event JSON dict and a Site row in Python, not in SQL. SQLModel
(matching Phase 2's newer style, since pipeline.py already needs a SQLModel Session in
scope to call phase2.store/phase2.scoring), not Phase 1's SQLAlchemy-declarative style.

P3Decision -- table `p3_decision`
id, canonical_id, stage, confidence_bucket, site_id  (unique together)
decision ("ALERT"|"SILENCE"), reasons (json list), metric, score,
components (json: {name: {score, weight, basis}} -- this IS the physical evidence
trail, basis cites real field values), government_evidence (json, passthrough from
Phase 1's Event.evidence), decided_at

Note: SQLModel tables share one process-wide metadata, so `phase3.db.init_db()` will
also create Phase 2's (empty, unused) `p2_*` tables if `phase2.models` has already been
imported in the process, and vice versa. Harmless -- idempotent DDL, no cross-writes,
each engine only ever queried through its own session -- but worth knowing before
wondering why a table shows up somewhere unexpected.

API surface
POST /v1/decide  (phase3/router.py) -- named to match the contract Phase 2's own
ideation doc already assumed. Body: `{event_id: str}` OR `{event: <Phase1 JSON>}`, plus
`site_id`. Returns the MaterialityDecision contract (phase3/schemas.py): decision,
reasons, government_evidence, metric/score/physical_components, replayed, evaluated_at.

Merge path
`phase3/app.py` is the combined process: `app.include_router(phase2.router)`,
`app.include_router(phase3.router)`, and Phase 1's standalone `FastAPI()` app is mounted
at `/phase1` (it's a module-level app, not an APIRouter like Phase 2/3 -- mounted rather
than refactoring Phase 1's already-tested module to match the router convention).

Responses to open asks

To Phase 1 (context/phase1.md sec. 8):
- "Reconcile stage vocabulary" -- see D1. Phase 3 keeps all six stages; the
  alert-eligibility gate is `{ADOPTED, REJECTED}`.
- "Agree the confidence type" -- see D2. `confidence_bucket()` is the float->string
  mapping, `MIN_CONFIDENCE = 0.6` is the alert floor.

To Phase 2 (context/phase2.md):
- "send canonical_id, not id" -- done; `pipeline.decide()` reads `event["canonical_id"]`
  and Phase 2's own TTL cache (unrelated call path) already coalesces repeat fetches for
  the same site+fields regardless.
- "absent is not missing" -- honored explicitly in phase2/scoring.py's `_clear_component`
  (see D6) and asserted by tests/test_scoring.py.
- "Decisions taken: Phase 2 computes defaults, Phase 3 may override" -- honored; see D6.
- Recommended bundle mapping table -- implemented as a keyword classifier
  (bundle_map.bundles_for), not a literal lookup; see D4.

Risks
R1 -- POINT_RADIUS_KM (1.5) and ALERT_THRESHOLD (0.5) are both unvalidated
placeholders, same class of risk as Phase 1's own R1 (fallback canonical_id collision,
unreviewed against real data) and Phase 2's parcel-trap lint (a lint test is the only
thing standing between a careless change and a real cost/correctness regression) --
worth tuning against real coordinate and event data before a real demo, not just the
two hand-authored fake profiles.

R2 -- Component weights in phase2/scoring.py (D6) are read from the ideation doc's
described bands, not from a calibrated `ScoreProfile` row in the database. The
`ScoreProfile` table and the `weights=` override parameter both exist and are wired,
but nothing seeds a non-default profile yet -- Phase 3 always evaluates against the
hardcoded defaults today.

R3 -- POLYGON geography is not evaluated (D3) -- conservatively treated as unresolved.
Low practical exposure today (Phase 1 never emits it without a geocoder), but if one is
added later this needs real point-in-polygon logic, not the current placeholder.

Progress
Step | State
Merge phase1 + phase2 onto a fresh phase3 branch | done, 68 pre-existing tests pass unmodified
Fix subject propagation (Phase 1 D9, was undone) | done
Phase 2 site registry + fetch orchestrator + bundle endpoints + fetch-log | done (was previously undone -- steps 4-6 of phase2.md's own progress table)
Phase 2 derived optionality scores (phase2/scoring.py) | done, HTTP route deferred
Phase 3 stage/confidence/geography gates | done
Phase 3 bundle selection + physical scoring + dedup | done
Phase 3 POST /v1/decide + combined app | done
Fake Mireye transport, two discriminating site profiles | done
End-to-end demo script (scripts/run_pipeline.py) | done
False-positive test coverage | done -- 124 tests total, 0 failing
Live Mireye API verified (real token, real fetches) | done -- see D7
Dual LLM provider (OpenAI primary, Gemini fallback) | done -- see D8

Test suite: 124 tests, all passing, no network/LLM/Mireye key required (dispatcher/
fallback logic is mocked at the provider-function boundary, not hitting real APIs).
Run: `pytest -q`.
Demo: `python scripts/run_pipeline.py`.
Serve the combined API: `uvicorn phase3.app:app --reload --port 8000`.
