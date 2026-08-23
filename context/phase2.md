# Phase 2 — Mireye Physical Intelligence & Datapoint Backend

**Branch:** `phase2` · **Owner:** Person 2 · **Status:** build steps 0–3 complete, 50 tests passing

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

| Endpoint | Returns |
|---|---|
| `POST /v1/sites` | register a monitored site; geocodes once, ever |
| `GET /v1/sites/{id}/{bundle}` | a bundle of physical datapoints, full provenance |
| `GET /v1/sites/{id}/derived/{metric}` | optionality scores (`data_center_optionality`, `bess_optionality`, `buildability`) |
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

**→ Phase 1: please keep `subject` in the emitted event.** Your Step 5 LLM schema captures
it; your Step 10 interface object drops it. Without it, Phase 3 cannot tell a data-centre
moratorium from a BESS one — and `fiber_broadband_available` is decisive for the first and
noise for the second. One-line fix.

**→ Phase 1 + Phase 3: stage vocabulary mismatch.** Phase 1 emits six stages
(`PROPOSED HEARD ADOPTED REJECTED WITHDRAWN TABLED`); Phase 3 accepts four
(`proposed heard adopted unknown`). `REJECTED` is not `unknown` — a rejected data-centre
moratorium *restores* option value and is a legitimate positive-direction alert. Needs a
three-way decision.

**→ Phase 3: send `canonical_id`, not `id`.** Physical facts do not change between June 1
and June 9, so the three stage transitions of one bill should reuse one cached snapshot.
Phase 2's TTL cache handles this automatically as long as you call with the same site.

**→ Phase 3: `absent` is not missing.** Mireye's field status is tri-state:
`ok` | `absent` | `failed`. `intersects_wetland: absent` means *there is no wetland here* —
evidence that RAISES optionality. Treating it as missing inverts the decision. Phase 2
preserves the distinction; please do not collapse it.

---

## Decisions taken

| Question | Decision |
|---|---|
| Datapoint selection | **Named bundles.** Phase 3 never reads the 366-field catalog. |
| Cache-miss behaviour | **GET auto-fetches.** No budget gate; quote + log kept as evidence. |
| Derived scores | **Phase 2 computes defaults; Phase 3 may override weights/thresholds.** |
| Packaging | **Mountable router.** Standalone today, `include_router` after merge. |

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
| 4 · Site registry + geocode + boundaries | blocked on `MIREYE_API_TOKEN` for live calls |
| 5 · Fetch orchestrator (quote → fetch → normalize → store → log) | next |
| 6 · Bundle endpoints with cache-miss auto-fetch | next |
| 7 · **Seattle spread test, 8 coordinates (~150 credits)** | de-risks the demo premise |
| 8 · Derived scores + profile override | pending |
| 9 · Raw field escape hatch + `/v1/fetch-log` | pending |

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
