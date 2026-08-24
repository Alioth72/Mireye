# Phase 2 — Mireye Physical Intelligence & Datapoint Backend

**Branch:** `phase2` (merged into `phase3`) · **Owner:** Person 2 · **Status:** all 9 build steps
complete (steps 4–9 were finished during the phase1+phase2 merge into `phase3`, not by Person 2 —
see the Progress table below and `context/phase3.md`), 66 tests passing within this subsystem.
See `context/phase3.md` for the integration layer and `context/GLOBAL.md` for the whole system.

---

## What Phase 2 is

A **data service**. It answers one question, and it has no concept of an event:

> **What is physically true at this coordinate, and how do we know?**

```text
Phase 1  ──  what happened in the public record        (events)
Phase 2  ──  what is physically true at this location   (datapoints)   <-- this file
Phase 3  ──  does that event matter at that location    (decision)
```

Phase 3 is the only component that holds both halves at once. Phase 2 is called *by*
Phase 3 and never sees an `event_type`, a `stage`, or a `canonical_id`.

---

## What Phase 1 and Phase 3 need to know

### Phase 2 will NOT give you

- `relation_to_site` or `distance_m` — we do not know where the event is
- A materiality score, impact direction, or stage language
- Anything that takes an `event_type` parameter

Scope resolution (event geography × site coordinate → relation) belongs to **Phase 3**.
It determines whether and where to fetch, which makes it a decision, not a lookup.

### Phase 2 WILL give you

All rows below are now real, built during the phase1+phase2 merge (see Progress) — this
table was originally aspirational when first written, it isn't anymore.

| Endpoint | Returns |
|---|---|
| `POST /v1/sites` | register a monitored site; geocodes once, ever |
| `GET /v1/sites/{id}/{bundle}` | a bundle of physical datapoints, full provenance |
| `GET /v1/sites/{id}/derived/{metric}` | optionality scores (`data_center_optionality`, `bess_optionality`, `buildability`) — **caveat:** the scoring logic (`phase2/scoring.py`) is real and tested, but this specific HTTP route was deliberately deferred; Phase 3 calls the scoring function in-process instead (`context/phase3.md` D6) |
| `GET /v1/sites/{id}/boundaries` | `political_region/county/locality`, `tract_geoid` — **use these for scope resolution instead of shipping shapefiles** |
| `POST /v1/quote` | exact cost of a selection, free and unmetered |
| `GET /v1/fetch-log` | credit audit trail — the demo evidence that N events cost N fetches |

### Bundles

Named after **physical systems**, not event types. There is no `data_center_moratorium`
bundle — choosing which bundle an event calls for is a materiality judgement and belongs
to Phase 3.

| Bundle | Credits | Covers |
|---|---|---|
| `grid` | 6 | transmission voltage/distance, substation, redundancy, interconnect queue |
| `telecom` | 3 | fiber availability, provider count, 5G |
| `terrain` | 3 | slope, elevation, grading difficulty |
| `water` | 4 | floodplain, FEMA zone, wetland |
| `constraints` | 3 | protected area, critical habitat, conservation easement |
| `access` | 3 | major road distance/class, rail |
| `boundaries` | 4 | political region/county/locality, tract |

**Recommended mapping (Phase 3 owns this table, Phase 2 just publishes it):**

| Event type | Subject | Bundles | Credits |
|---|---|---|---|
| moratorium | data center | grid + telecom + terrain + water + constraints | 19 |
| moratorium | BESS / battery | grid + terrain + water + constraints | 16 |
| rezoning / annexation / comp-plan | any | terrain + water + constraints + access | 13 |
| utility extension | sewer / water | terrain + water + access | 10 |
| utility extension | power | grid + terrain + access | 12 |
| major development permit | any | terrain + water + constraints + access | 13 |

---

## Cross-phase asks

**→ Phase 1: please keep `subject` in the emitted event.** ✅ **RESOLVED**, but not as
smoothly as first reported: Phase 1's own doc claimed this was fixed well before it
actually was (the storage/API side was never wired, only the extraction prompt). Actually
fixed during the phase1+phase2 merge into `phase3` — see `context/phase1.md` D9's
2026-08-24 correction. Now genuinely flows end to end, covered by tests.

**→ Phase 1 + Phase 3: stage vocabulary mismatch.** ✅ **RESOLVED** by Phase 3
(`context/phase3.md` D1): Phase 1 kept all six stages, no collapsing, exactly as this ask
anticipated might be needed. Phase 3 owns an alert-eligibility gate instead of a
vocabulary mapping — only `ADOPTED`/`REJECTED` are alert-eligible, `REJECTED` included
deliberately for the reason given here (restores option value).

**→ Phase 3: send `canonical_id`, not `id`.** ✅ Honored — `phase3/pipeline.py`'s
`decide()` reads `event["canonical_id"]` and it's also the dedup key for Phase 3's own
decision cache (`context/phase3.md` D5), so the "one bill, three stage transitions, one
physical evaluation" property holds on both sides now, not just Phase 2's TTL cache.

**→ Phase 3: `absent` is not missing.** ✅ Honored on the consuming side too, not just
preserved on this one: `phase2/scoring.py`'s `_clear_component` (built during the merge,
implementing the derived-scores contract this doc's own "Decisions taken" table already
committed to) explicitly treats `absent` as confirmed clearance, `ok`+true as the
penalty, and `failed`/missing as a distinct third case — never collapsed into either.
Three-way behavior asserted directly in `tests/test_scoring.py`.

---

## Decisions taken

| Question | Decision |
|---|---|
| Datapoint selection | **Named bundles.** Phase 3 never reads the 366-field catalog. |
| Cache-miss behaviour | **GET auto-fetches.** No budget gate; quote + log kept as evidence. |
| Derived scores | **Phase 2 computes defaults; Phase 3 may override weights/thresholds.** Built during merge as `phase2/scoring.py`; `GET /v1/sites/{id}/derived/{metric}` HTTP route deliberately deferred (contract honored in-process, nothing calls it over the wire yet) — see `context/phase3.md` D6. |
| Packaging | **Mountable router.** Standalone today, `include_router` after merge — done exactly as planned: `phase3/app.py` does `app.include_router(phase2.router)` verbatim. |

---

## Merge path

Everything hangs off one `APIRouter` with no module-level app. Tables are prefixed `p2_`,
config is namespaced `MIREYE_*` / `PHASE2_*`. At merge, Phase 3 does:

```python
app.include_router(phase2.router)
```

`phase2/app.py` is the only file the merge may discard.

---

## Progress

| Step | State |
|---|---|
| 0 · Scaffold, router layout, config | done |
| 1 · Catalog mirror, `/v1/quote`, `/v1/budget` | done |
| 2 · Datapoint store: tri-state, TTL, never-cache-a-failure | done |
| 3 · Bundle definitions + parcel-trap lint | done |
| 4 · Site registry + geocode + boundaries | done (merge) — `MIREYE_API_TOKEN` obtained and verified live: `POST /v1/sites` correctly geocodes/registers and pulls the `boundaries` bundle once |
| 5 · Fetch orchestrator (quote → fetch → normalize → store → log) | done (merge) — `phase2/orchestrator.py`, verified against the live API (not just the fake transport): real terrain + boundaries fetches, correct license resolution on real Overture/Census data |
| 6 · Bundle endpoints with cache-miss auto-fetch | done (merge) — `GET /v1/sites/{id}/{bundle}` |
| 7 · **Seattle spread test, 8 coordinates (~150 credits)** | **not done for real** — see updated Open Risk section below; a 2-coordinate hand-authored *fake* stand-in was built instead for demo reproducibility |
| 8 · Derived scores + profile override | done (merge) — `phase2/scoring.py`; HTTP route (`GET /v1/sites/{id}/derived/{metric}`) still deferred, see Decisions Taken |
| 9 · Raw field escape hatch + `/v1/fetch-log` | `/v1/fetch-log` done (merge); the raw per-field escape-hatch endpoints (`GET /v1/sites/{id}/datapoints[/​{field}]`, `POST .../datapoints:refresh`) are still not built |

---

## Open risk the whole team should see

**Seattle may not discriminate physically.** Phase 3's winning story is *"we stayed quiet
on non-buildable ground."* That needs monitored sites whose physical fields actually
diverge. Seattle city limits is dense and urban — near-universal fiber, short substation
distances, little wetland, almost nothing protected. If every site scores high optionality,
Phase 3 never emits a `quiet`, and the one thing separating this from a keyword feed never
appears on stage.

Seattle does have real variation (Duwamish floodplain, West Seattle and Magnolia steep
slopes), so this is testable rather than fatal. Step 7 answers it for ~150 credits in one
afternoon. If the scores do not separate, the fix is adding King County for rural ground —
Build Brief II permits "a county, a town, or a single address," so widening is not a scope
violation.

**Status update (phase3 integration, 2026-08-24) — still open, addressed for the demo, not
for the real question.** Step 7 (the real 8-coordinate Seattle spread test against the
live API) was never run. Instead, `tests/fakes/mireye_fake.py` hand-authors two synthetic
coordinate profiles (a SODO-industrial-shaped "good" one, a Duwamish-floodplain-shaped
"bad" one) specifically constructed to land on opposite sides of the ALERT threshold —
confirmed by `phase2/scoring.py` to score 1.0 vs 0.009 on `data_center_optionality`. This
proves the *scoring math* discriminates correctly when given genuinely different inputs,
and it's what `scripts/run_pipeline.py`'s demo runs against by default (reproducible, free,
no live API dependency — see `context/phase3.md` D7). It does **not** prove Seattle's real
physical data actually spreads the way this risk worries about. One real data point exists
in favor: a live `boundaries` fetch during MIREYE_API_TOKEN verification confirmed
GOOD_SITE's coordinates really do resolve to Seattle/King County via Overture/Census — but
that's boundary data, not the terrain/grid/water fields this risk is actually about. The
real spread test (~150 credits, live API, all 7 physical bundles across 8 real
coordinates) is still worth running before treating this risk as closed.
