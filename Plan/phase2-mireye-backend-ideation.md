# Phase 2 — Mireye Data Service & Datapoint Backend

**Owner:** Person 2
**Branch:** `phase-2-mireye` — merges into Phase 3 later
**Consumes:** a coordinate (or address) and a bundle name. **No events.**
**Produces:** provenance-tagged physical datapoints and derived optionality scores, over HTTP.
**Reference:** `mireye-api-reference.md` (catalog 0.16.0), `mireye-du-monitor-brief.md`

---

## 0. Decisions taken

| Question | Decision |
|---|---|
| How Phase 3 selects datapoints | **Named bundles** (`/grid`, `/water`, …). Phase 3 never reads the 366-field catalog. |
| Cache-miss behaviour | **GET auto-fetches.** Credits are not scarce; no budget gating. Quote + log retained as demo evidence, not as a brake. |
| Derived optionality scores | **Phase 2 computes defaults; Phase 3 may override weights and thresholds.** |
| Packaging | **Own branch, mountable router.** Runs standalone today, mounts into Phase 3's app at merge. |

---

## 1. What this phase is — and what it is deliberately not

Phase 2 is a **data service**. It turns Mireye's 366-field catalog into a small, cached,
provenance-preserving HTTP API over the physical facts of a monitored location.

**Phase 2 does not know what an event is.** No `event_type`, no `stage`, no `canonical_id`, no
jurisdiction geometry. Phase 1 produces events; Phase 3 combines events with the datapoints this
service exposes and decides whether to alert. That combining step is Phase 3's `POST /v1/decide`
and it stays there.

```text
Phase 1  ──  what happened in the public record        (events)
Phase 2  ──  what is physically true at this location   (datapoints)   <-- this doc
Phase 3  ──  does that event matter at that location    (decision)
```

Phase 3 is the only component holding both halves at once. Phase 2 answers a question with no
event in it: **what is true at this coordinate, and how do we know?**

---

## 2. Scope boundary

**In scope**

- Site registry — address to coordinate, geocoded exactly once, with geocode provenance
- Datapoint store — tri-state status, TTL-honoring cache, per-field provenance and license
- Bundle endpoints, raw field access, derived optionality scores with override support
- Fetch orchestration against Mireye: quote, fetch, normalize, store, log
- Catalog mirroring and local field-name validation

**Out of scope — other people's phases**

- Scraping, parsing, classifying, deduplicating government records → Phase 1
- Scope resolution (event geography x site → relation + distance) → Phase 3
- Choosing which bundle an event calls for → Phase 3 (we publish a recommendation, sec. 10.1)
- Materiality scoring, alert/quiet/review, thresholds, stage language → Phase 3
- Parcel/ownership fields — 300 credits per location, excluded by the brief
- Frontend, dashboards, alert delivery

**On credits.** The account has room, so there is no budget gate and a cache miss simply
fetches. But Build Brief II grades the architecture on credit discipline — *"if you are burning
credits, your agent is polling and the architecture is wrong"* — so two habits stay, because
both are free:

- **Quote before every fetch.** `/v1/fetch/quote` is unmetered and exact, computed by the same
  code that charges. It costs one extra call and makes every spend predicted rather than
  discovered.
- **Log every fetch.** `GET /v1/fetch-log` is how the team proves on stage that N events cost N
  fetches and not N x documents. That is a scoring criterion, not ops hygiene.

The TTL cache does the real work. One bill moving `PROPOSED → HEARD → ADOPTED` hits the same
site and the same fields, so calls two and three are cache hits — without Phase 2 knowing those
three requests share an event. Physical facts do not change between June 1 and June 9; the TTL
already encodes that.

---

## 3. Architecture

```text
                Phase 3 (orchestrator, holds the event)
                              |
                              |  GET /v1/sites/{id}/grid
                              |     cache hit  -> served, 0 credits
                              |     cache miss -> quote, fetch, store, serve
                              v
        +-----------------------------------------------+
        |     Phase 2 data service  (mountable router)   |
        |                                               |
        |   Site Registry ──── geocode once, ever        |
        |         |                                      |
        |   Datapoint Store ── (site, field) + TTL       |
        |         |            tri-state status          |
        |         |            provenance + license      |
        |         |                                      |
        |   Derived Scores ─── defaults + overrides      |
        |         |                                      |
        |   Fetch Orchestrator ── quote -> fetch -> log  |
        +-----------------------------------------------+
                              |
                              v
                      Mireye Earth API
```

Every inbound request names a **site** and a **bundle**. Nothing carries an event.

---

## 4. Data model

SQLite for the hackathon. Nothing here needs PostGIS.

### `site`

```text
id                  uuid
label               text
address_raw         text nullable
lat, lng            float          -- authoritative
geocode_accuracy    text nullable
accuracy_type       text nullable  -- matters MORE than accuracy
parcel_grade        bool nullable  -- false => coordinate may be a neighbour's
geocode_provider    text nullable
geocoded_at         timestamptz
political_region    text nullable  -- from boundaries bundle, sec. 7
political_county    text nullable
political_locality  text nullable
tract_geoid         text nullable
created_at          timestamptz
```

The four `political_*` / `tract_geoid` columns are Mireye datapoints in their own right. Phase 3
uses them to answer "is this site inside the jurisdiction that acted." We store and serve them;
we do not interpret them.

### `datapoint`

One row per (site, field). The cache, and the source of truth behind every endpoint. Mirrors
Mireye's per-field response record exactly — nothing is flattened away.

```text
id                uuid
site_id           uuid
field_name        text
value             json           -- null when status != 'ok'
unit              text nullable
status            text           -- 'ok' | 'absent' | 'failed'  <-- TRI-STATE, never collapse
error             text nullable  -- only when failed
retryable         bool nullable  -- only when failed
source            text
source_url        text
license           text nullable  -- derived from runtime source at write time; ODbL, sec. 7
confidence        text           -- 'high' | 'medium' | 'low' | 'unknown'
fetched_at        timestamptz    -- authoritative as-of, NOT created_at
dataset_vintage   text nullable
ttl_seconds       int nullable
notes             text nullable
request_id        text           -- X-Request-ID, for support correlation
UNIQUE (site_id, field_name)
```

**Hard rule one.** `status = 'failed'` is never cached as an answer. HTTP is 200 on a failed
field, so a naive upsert freezes a failure as truth. Cache `ok` and `absent` to `ttl_seconds`;
re-fetch `failed` with backoff only when `retryable`.

**Hard rule two.** `absent` is a real answer and bills normally. `intersects_wetland: absent`
means *there is no wetland here* — evidence that raises optionality, not a missing field.
Phase 3 scores on this distinction, so we must not destroy it in transit.

### `fetch_log`

```text
id                uuid
site_id           uuid
fields            text[]
quoted_credits    int
charged_credits   int nullable   -- may be lower than quoted (refunds)
credits_remaining int nullable
request_id        text
idempotency_key   text nullable
trigger           text           -- 'registration' | 'cache_miss' | 'refresh' | 'replay'
caller_ref        text nullable  -- opaque; Phase 3 may pass an event id for its own audit
created_at        timestamptz
```

`caller_ref` is deliberately opaque. Phase 3 can put a canonical event id there so the log can
be read per-event at demo time; Phase 2 never parses it.

### `score_profile`

Named weight/threshold sets for derived scores (sec. 5.4). Ships with a `default` row per
metric; Phase 3 can POST its own after replay calibration.

```text
id            uuid
metric        text     -- 'data_center_optionality' | 'bess_optionality' | 'buildability'
name          text     -- 'default' | 'replay_tuned_v2' | ...
weights       json
thresholds    json
created_at    timestamptz
UNIQUE (metric, name)
```

### `field_catalog`

Local mirror of `GET /v1/meta/fields` (public, ETag-cached, 1 h). Validates field names before
sending, so `400 fields_unknown` never reaches production.

---

## 5. The endpoints

### 5.1 Site management

```http
POST   /v1/sites              register a site (address or lat/lng)
GET    /v1/sites              list
GET    /v1/sites/{id}         detail incl. geocode provenance
DELETE /v1/sites/{id}
```

`POST /v1/sites` is the only place geocoding happens — 1 credit, cached 30 days upstream and
forever locally. It also pulls the `boundaries` bundle (4 credits), since Phase 3 needs those
values for scope resolution and they never change.

If the geocode returns `parcel_grade: false`, the response carries a loud warning and the site
is flagged; every datapoint served for that site is marked degraded. The coordinate may sit on a
neighbour's parcel, and slope or floodplain on the wrong parcel is a confidently wrong answer
with no symptom.

### 5.2 Bundles — the primary path

Bundles are named after **physical systems**, not event types. There is no
`data_center_moratorium` endpoint, and that naming is the phase boundary made visible.

```http
GET /v1/sites/{id}/grid           power interconnect picture
GET /v1/sites/{id}/telecom        fiber / connectivity
GET /v1/sites/{id}/terrain        buildability of the ground
GET /v1/sites/{id}/water          flood + wetland exposure
GET /v1/sites/{id}/constraints    protected / habitat / easement
GET /v1/sites/{id}/access         roads and rail
GET /v1/sites/{id}/boundaries     political region / county / locality / tract
```

| Bundle | Fields | Credits on miss |
|---|---|---|
| `grid` | `nearest_transmission_line_voltage_kv`, `nearest_transmission_line_distance_m`, `nearest_substation_distance_m`, `max_transmission_line_voltage_kv_within_radius`, `transmission_redundancy_flag`, `interconnection_queue_active_capacity_county_mw` | 6 |
| `telecom` | `fiber_broadband_available`, `fiber_provider_count`, `mobile_5g_coverage_class` | 3 |
| `terrain` | `slope_degrees`, `elevation`, `grading_difficulty_class` | 3 |
| `water` | `within_floodplain_polygon`, `fema_flood_zone`, `intersects_wetland`, `wetland_acres` | 4 |
| `constraints` | `intersects_protected_area`, `intersects_critical_habitat`, `intersects_conservation_easement` | 3 |
| `access` | `nearest_major_road_distance_m`, `nearest_major_road_class`, `nearest_rail_line_distance_m` | 3 |
| `boundaries` | `political_region`, `political_county`, `political_locality`, `tract_geoid` | 4 |

**Cache-miss behaviour.** A `GET` serves cached fields where they are fresh and fetches the rest
in one Mireye call — quoting first, logging after. The response always states what happened:

```json
{
  "bundle": "grid",
  "site_id": "...",
  "cache": {"hits": 4, "fetched": 2, "credits_spent": 2, "quoted": 2},
  "datapoints": []
}
```

Phase 3 never has to make a second call, and the cost of every read is still visible in the
response and in the log.

**Parcel-trap check.** `intersects_protected_area`, `intersects_critical_habitat`, and
`intersects_conservation_easement` live in the `parcels` *layer* (43 fields) but are **not**
members of the 19-field `parcel_record` metered group — 1 credit each, confirmed by the
reference's cost-discipline section. The five that silently drag in the 300-credit record are
`wetland_acres_on_parcel`, `wetland_fraction_of_parcel`, `developable_acres_proxy`, and both
`onsite_solar_potential_mwac_*`. **None appear in any bundle above, and a lint test keeps it
that way.** This matters more now than under budget gating: nothing else would stop it.

### 5.3 Raw field access — the escape hatch

```http
GET  /v1/sites/{id}/datapoints                 everything held for this site
GET  /v1/sites/{id}/datapoints/{field_name}    one field, full provenance
POST /v1/sites/{id}/datapoints:refresh         force refetch even when fresh
```

```json
{
  "field": "nearest_transmission_line_voltage_kv",
  "value": 230,
  "unit": "kV",
  "status": "ok",
  "source": "EIA Energy Atlas",
  "source_url": "https://...",
  "license": null,
  "confidence": "high",
  "fetched_at": "2026-08-22T12:00:00Z",
  "dataset_vintage": "2026-06",
  "ttl_seconds": 86400,
  "stale": false,
  "notes": null
}
```

`stale` is computed locally: `now - fetched_at > ttl_seconds`. `:refresh` exists for the case
where a value is cached and fresh but we want it re-pulled anyway — a bad fetch, a gated field
going live mid-hackathon.

### 5.4 Derived optionality scores

The brief's central move: *"No field is called `had_data_center_optionality` — derive it."*
A pure function of the physical facts at a coordinate, with no event input.

```http
GET  /v1/sites/{id}/derived/{metric}                 default profile
GET  /v1/sites/{id}/derived/{metric}?profile=name    a stored profile
POST /v1/sites/{id}/derived/{metric}                 inline weights/thresholds, no storage
POST /v1/score-profiles                              store a named profile
GET  /v1/score-profiles                              list, incl. the shipped defaults
```

Metrics: `data_center_optionality`, `bess_optionality`, `buildability`.

```json
{
  "metric": "data_center_optionality",
  "profile": "default",
  "score": 0.81,
  "confidence": "medium",
  "components": {
    "power":   {"score": 0.95, "weight": 0.4, "basis": "230 kV at 1.2 km, substation 3.4 km"},
    "fiber":   {"score": 1.0,  "weight": 0.2, "basis": "fiber_broadband_available = true"},
    "terrain": {"score": 0.88, "weight": 0.2, "basis": "slope 3.1 deg"},
    "clear":   {"score": 0.5,  "weight": 0.2, "basis": "wetland unknown (status=failed)"}
  },
  "fields_used": [],
  "fields_missing": ["intersects_wetland"],
  "citations": []
}
```

**Defaults:**

- `power` — voltage banded (`>=230 kV` full, `115-230` high, `69-115` partial, `<69` or absent
  near zero), attenuated by substation distance
- `fiber` — binary, weighted heavily for `data_center_optionality`, **absent entirely from
  `bess_optionality`**
- `terrain` — slope inverse-banded (`<5deg` full, `5-10deg` partial, `>15deg` near zero)
- `clear` — multiplicative penalty for floodplain / wetland / protected area

The composite is **multiplicative, not additive**. A site with no power has zero data-center
optionality no matter how flat it is. Additive weights would score flat unpowered farmland as a
near-miss and Phase 3 would alert on it — exactly the failure mode the brief calls "the keyword
feed." **An override profile can change weights and thresholds; it cannot change the composition
rule**, because that guard is the difference between the product and a keyword feed.

**Every response names its profile.** With overrides in play, "the number in the alert doesn't
match the number the API returns" is a real demo-night failure, so `profile` is echoed on every
score and recorded by Phase 3 alongside the decision.

**Boundary note.** These are *capability* measures — "could this ground host a data center" —
never materiality. The second question needs the event and belongs to Phase 3.

### 5.5 Ops

```http
POST /v1/quote           proxy to Mireye quote — free, unmetered, exact
GET  /v1/budget          credits used / remaining / resets_at  (informational)
GET  /v1/catalog/fields  mirrored field catalog
GET  /v1/fetch-log       full audit trail — the demo evidence
GET  /healthz
```

---

## 6. Two cheap tricks worth building in early

**Jurisdiction membership without shapefiles.** Phase 3 needs "is this site inside Seattle /
King County" for scope resolution. Rather than making them ship boundary geometry, we pull the
`boundaries` bundle once at registration — 4 fields, Census TIGER, ~1 year TTL — and serve it at
`GET /v1/sites/{id}/boundaries`. They do the comparison; we supply the fact.

**Attribution carried, not reconstructed.** Overture Transportation/Buildings/Divisions and
OpenInfraMap/OSM fields are **ODbL — attribution plus share-alike on derived values**. Our
optionality scores *are* derived values and an alert is redistribution, so the obligation reaches
Phase 3's output. Store `license` on the datapoint row at write time, from the **runtime**
`source`. Never infer it from the field name: `nearest_transmission_line_*` is EIA/HIFLD while
`nearest_osm_transmission_line_*` is ODbL, and runtime provenance can differ from the catalog
default — the reference documents `elevation` falling back from `USGS_EPQS` to `USGS_3DEP_COG`.

---

## 7. Packaging and merge path

Phase 2 lives on branch `phase-2-mireye` and merges into Phase 3 later. To make that merge
boring:

```text
phase2/
  router.py        APIRouter with prefix="/v1", no app object
  app.py           thin standalone FastAPI for solo dev — the only file merge may discard
  models.py        SQLModel tables (site, datapoint, fetch_log, score_profile, field_catalog)
  bundles.py       bundle -> field-list definitions
  scoring.py       derived metrics + profile resolution
  mireye/          API client: quote, fetch, geocode, catalog
  config.py        settings, MIREYE_API_TOKEN
```

- **Everything hangs off one `APIRouter`.** Post-merge, Phase 3 does
  `app.include_router(phase2.router)` and nothing else changes.
- **No module-level app, no `@app.on_event`.** Lifespan work goes in a function Phase 3 can call.
- **Table names prefixed** (`p2_site`, `p2_datapoint`, …) so a shared database has no collisions.
- **All config under a `MIREYE_` / `PHASE2_` prefix** so merged env files do not fight.
- **Phase 3 talks to Phase 2 over HTTP now, in-process after merge.** Keeping the call surface
  to the endpoints in sec. 5 means that switch is a base-URL change, not a rewrite.

---

## 8. Backend stack

FastAPI + Pydantic v2 + SQLModel + SQLite + `httpx.AsyncClient`. Matches Phase 1's Python.

**Client rules that are not optional:**

| Call | Client timeout |
|---|---|
| `/v1/fetch` (coordinate) | 30 s |
| `/v1/fetch` (address form) | +5.5 s on top |
| `/v1/fetch/batch` | >=120 s |
| `/v1/geocode` | >5.5 s |

A too-short timeout does **not** cancel server-side work — it keeps running and billing.

- Honor `retryable`, not the HTTP status code. Retryable failures carry `Retry-After`.
- Send our own `X-Request-ID` and store it. Unhandled 500s carry no body and no header; the
  request id is the only way to correlate with Mireye's logs.
- Startup: `GET /v1/meta/fields` (public, ETag, cache 1 h), `GET /v1/meta/plans`,
  `GET /v1/users/me/usage`. Validate field names locally before sending.
- Auth: dashboard API token in `MIREYE_API_TOKEN`, 90-day lifetime. MCP OAuth tokens are **not**
  accepted on `/v1/*`.
- Coordinate validation must accept the Western Aleutian box as well as the primary rectangle;
  `us_envelope` in `/v1/meta/fields` omits it for back-compat.
- **A cache miss on a GET now triggers a network call.** Every bundle endpoint needs the fetch
  timeout budget above, not a default 5 s, or Phase 3 sees spurious failures under load.

---

## 9. Replay support

Replay is Phase 1's and Phase 3's exercise. Phase 2's obligation is to keep the timeline honest:
every datapoint carries `fetched_at`, every response exposes `stale`.

**The limitation to state out loud.** Mireye returns fields as-of-now, not as-of-event.
Replaying a June 2026 moratorium in August 2026 scores it against August data. For most fields
this is nearly harmless — Census TIGER boundaries and USGS terrain carry ~1 year TTLs, floodplain
and wetland geometry barely move — but EIA Atlas (transmission, substations) has a **1 day** TTL
and genuinely could differ.

Phase 2 surfaces `fetched_at`; Phase 3 compares it against the event date and decides what to
say. State the caveat in the demo rather than hide it. The whole product claim is about dates,
and a judge who hears "these are current-state fields and here is the exposure" trusts the
lead-time number more, not less.

---

## 10. Contract with Phase 3

```text
1. Phase 3 receives a canonical event from Phase 1
2. Phase 3 resolves scope        (its job: event geometry x site -> relation, distance_m)
3. Phase 3 picks bundles for the event type   (its mapping, sec. 10.1)
4. Phase 3 calls GET /v1/sites/{id}/{bundle}  -- served from cache or fetched, one call
5. Phase 3 calls GET /v1/sites/{id}/derived/{metric}[?profile=...]
6. Phase 3 combines event + datapoints -> POST /v1/decide -> alert | review | quiet
```

### 10.1 Recommended bundle mapping — Phase 3 owns this table, we publish it

Which fields an event calls for is a materiality judgment, so it belongs to Phase 3. This is our
recommendation from the catalog, so they need not read all 366 fields:

| Event type | Subject | Suggested bundles | Credits on full miss |
|---|---|---|---|
| moratorium | data center | grid + telecom + terrain + water + constraints | 19 |
| moratorium | BESS / battery | grid + terrain + water + constraints | 16 |
| moratorium | other | terrain + water + constraints + access | 13 |
| rezoning / annexation / comp-plan | any | terrain + water + constraints + access | 13 |
| utility extension | sewer / water | terrain + water + access | 10 |
| utility extension | power | grid + terrain + access | 12 |
| major development permit | any | terrain + water + constraints + access | 13 |

All well under the **50-field fetch cap**. Note `data_center_siting` (135 fields) and
`site_selection` (72) **cannot be fetched whole** — presets are exempt from the explicit-field
cap but the resolved set still caps at 50. We name fields; we never send a big preset.

**This table is why Phase 1 must keep `subject`.** Their Step 5 LLM schema captures it and their
Step 10 interface object drops it. Without it, Phase 3 cannot tell a data-center moratorium from
a BESS one, and `fiber_broadband_available` is decisive for the first and noise for the second.
One-line fix in their interface — raise it now.

### 10.2 What Phase 3 must not expect from us

- No `relation_to_site`, no `distance_m` — we do not know where the event is
- No materiality score, no impact direction, no stage language
- No inference across sites; each request names one site
- Nothing that takes an `event_type` parameter

---

## 11. Risks

**1. Seattle may not discriminate physically. Biggest risk, and it lands on our layer.**
Phase 3's winning story is *"we stayed quiet on non-buildable ground."* That needs sites whose
physical fields actually diverge. Seattle city limits is dense and urban: near-universal fiber,
short substation distances, little wetland, almost nothing protected. If every site scores high
optionality, Phase 3 never emits a `quiet` and the one thing separating the product from a
keyword feed never appears on stage. Seattle does have real variation — Duwamish floodplain,
West Seattle and Magnolia steep slopes — so this is testable rather than fatal.
**Mitigation, and the cheapest de-risk available to the whole team:** register 8 deliberately
spread Seattle coordinates and pull the optionality bundles. ~150 credits, one afternoon. If the
scores do not separate, add King County for rural ground with genuine transmission-versus-terrain
tension. Build Brief II permits "a county, a town, or a single address," so widening is not a
scope violation.

**2. Auto-fetch plus no budget gate removes every backstop against a runaway loop.** A retry
storm, a Phase 3 bug polling a bundle in a loop, or a `for site in sites` during debugging now
spends without anything stopping it. Mitigations, none of which is a budget gate: per-(site,
bundle) in-flight deduplication so concurrent identical GETs share one fetch; a negative cache on
`failed` so a broken field is not re-fetched on every request; and a loud log line when one site
is fetched more than N times an hour. Cheap to build, and they protect the fetch-log story that
Brief II actually grades.

**3. Gated catalog fields.** Some recent `climate`/`hazards`/`utilities`/`parcels` additions are
catalog-listed but not yet ingested — they return honest provenance nulls with an explanatory
`notes`. Verify every field in sec. 5.2 returns real values at a Seattle coordinate before demo
day.

**4. The `parcel_grade: false` trap.** A geocode landing on a neighbour's parcel produces a
confidently wrong slope or floodplain answer with no symptom. Flag at registration, mark every
datapoint for that site degraded, let Phase 3 route it to `review`.

**5. Score-profile drift.** With overrides live, the alert text and the API can disagree about a
score. Every derived response echoes `profile`; Phase 3 must record it with the decision.

**6. Someone re-absorbs the combining.** The failure mode the previous draft had. If an
`?event_type=` parameter ever appears on a Phase 2 endpoint, the boundary has leaked.

---

## 12. Build order

Each step is independently runnable and testable. Steps 0-3 spend nothing.

| # | Step | Credits | Proves |
|---|---|---|---|
| 0 | Scaffold, `APIRouter` layout, config, `MIREYE_API_TOKEN` | 0 | merge-ready shape from line one |
| 1 | Catalog mirror + `/v1/quote` proxy + `/v1/budget` | 0 | auth works, pricing is knowable |
| 2 | `datapoint` store: tri-state status, TTL, `failed`-never-cached test | 0 | the correctness core |
| 3 | Bundle definitions + parcel-trap lint test | 0 | we cannot accidentally spend 300 |
| 4 | Site registry + geocode + `boundaries` | 5/site | first real credits |
| 5 | Fetch orchestrator: quote, fetch, normalize, store, log | ~19 | end-to-end on one site |
| 6 | Bundle endpoints with cache-miss auto-fetch + in-flight dedup | 0 | the primary API path |
| 7 | **Seattle spread test across 8 coordinates** | ~150 | **answers Risk 1 while there is time to act** |
| 8 | Derived scores + profile override + `/v1/score-profiles` | 0 | what Phase 3 consumes |
| 9 | Raw field escape hatch + `/v1/fetch-log` | 0 | the demo evidence |

Step 7 is deliberately early. It is the only step that can invalidate the demo premise, and it
costs less than a day of casual development.

---

## 13. Open questions for the team

1. Does Phase 1 restore `subject` in its emitted event? Phase 3 cannot pick bundles without it.
2. Is the monitored-site list fixed for the demo, or registered live?
3. Is the anchor bill number for the Seattle data-center moratorium confirmed against Legistar,
   or still a placeholder? The spread-test coordinates should sit near whatever it actually
   covers.
4. At merge, does Phase 3 want Phase 2's tables in the same database file or a separate one?
   Prefixed table names work either way; this only decides the connection string.
