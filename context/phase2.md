# Phase 2 — Mireye Physical Intelligence & Datapoint Backend

**Branch:** `phase2` · **Owner:** Person 2
**Status:** build steps 0–3 complete · 50 tests passing · 0 credits spent
**Last updated:** 2026-08-23

---

## 0. How to read this file

This is the integration contract for Phase 2, written for Person 1 (public record) and
Person 3 (decision layer). The phases interlock but are built on separate branches, so
anything not written here gets discovered at integration time.

Section 3 is the important one. It records not just *what* was decided but *what was
rejected and why*, because you need to know which of my constraints are load-bearing and
which are arbitrary. If a decision blocks you, section 3 tells you what it costs to
reverse.

- **Section 3** — design decisions, with alternatives and reversal cost
- **Section 5–6** — the API you actually call
- **Section 7–8** — what I need from you, and what you must not expect from me
- **Section 9** — risks that affect the whole team

---

## 1. Position in the system

```text
Phase 1  ──  what happened in the public record        (events)
Phase 2  ──  what is physically true at this location   (datapoints)   <-- this file
Phase 3  ──  does that event matter at that location    (decision)
```

Phase 3 is the **only** component that holds both halves at once. That is deliberate and
it was not the original design — see D1 for the correction and why it matters.

Phase 2 answers exactly one question:

> **What is physically true at this coordinate, and how do we know?**

It never sees an `event_type`, a `stage`, a `canonical_id`, or event geometry.

---

## 2. Why this phase exists at all

Build Brief II's core claim is that record-watching products already exist and fire on the
word "rezoning", but none can answer **does this matter *here*** — a physical question. A
data-centre moratorium only destroys value where transmission, fiber, and buildable terrain
existed. A sewer extension only creates value where slope and floodplain allow building.

Mireye supplies the physical half. Phase 2 is the layer that turns 366 catalog fields into
a small, cheap, provenance-preserving answer, and it is also the layer that owns the credit
bill. The brief's rule is absolute:

> "A place produces a handful of material events a month; each costs one vicinity fetch.
> If you are burning credits, your agent is polling and the architecture is wrong."

Several decisions below exist only to make that sentence provably true.

---

## 3. Design decisions

Format: **Decision** → *Alternatives rejected* → *Rationale* → *Consequence* → *Reversal cost*.

---

### D1 — Phase 2 is event-blind

**Decision.** Phase 2 has no concept of an event. No `event_type` parameter exists on any
endpoint, and none may be added.

**Alternatives rejected.** The first draft of this design gave Phase 2 four event-aware
responsibilities: scope resolution (event geometry × site → relation), fetch gating (is
this event worth spending on), field selection keyed on `event_type` + `subject`, and a
`POST /v1/events/enrich` entry point. All four were removed.

**Rationale.** Those four things all require reading a Phase 1 event, which means Phase 2
was doing a first pass of the combining that is Phase 3's entire job. The distinction that
matters: there is *judgment* combining (event + physics → score → alert/quiet) and
*parameterisation* (using the event to pick which fields at which coordinate). The original
draft kept judgment out but pulled parameterisation in, on the assumption that
event-awareness was the only way to enforce "one fetch per event, not per document."
That assumption was wrong — see D2.

**Consequence.** Phase 3 must own scope resolution. This is not a formatting step: it
decides whether and where to fetch, which makes it the cost decision. Phase 3 also owns the
event→bundle mapping (section 6.2).

**Reversal cost.** High, and it should not be reversed. The tripwire: if a `?event_type=`
parameter ever appears on a Phase 2 endpoint, the boundary has leaked and the credit story
in D6 stops being provable. Treat that as a review blocker.

---

### D2 — Credit control via TTL cache, not an event-keyed constraint

**Decision.** Datapoints are cached on `(site_id, field_name)` against Mireye's own
`ttl_seconds`. There is no event id anywhere in the schema.

**Alternatives rejected.** An `enrichment` table with
`UNIQUE (canonical_event_id, site_id)`, enforcing exactly one fetch per event per site.

**Rationale.** Physical facts do not change between June 1 and June 9. A bill moving
`PROPOSED → HEARD → ADOPTED` hits the same site and the same fields, so calls two and three
are cache hits *whether or not we know they share an event*. The TTL cache subsumes the
event-keyed constraint entirely, and it does so without importing Phase 1's vocabulary.

**Consequence.** This is worth roughly 3× on the bill for a typical event lifecycle, and it
no longer depends on Phase 1's deduplication being correct. Under the rejected design, a
leak in Phase 1's canonicalisation would have silently tripled Phase 2's spend; now a
canonicalisation bug costs Phase 3 duplicate decisions but costs Phase 2 nothing.

**Reversal cost.** Low, but pointless. Adding event-keying back would reintroduce D1's leak.

---

### D3 — Bundles are named after physical systems, not event types

**Decision.** `grid`, `telecom`, `terrain`, `water`, `constraints`, `access`, `boundaries`.

**Alternatives rejected.** `data_center_moratorium`, `bess_moratorium`, `rezoning` — i.e.
bundles named for the event that motivates them.

**Rationale.** The naming *is* the boundary made visible. A `data_center_moratorium` bundle
would encode a materiality judgement (that a moratorium calls for these fields and not
those) inside a data service. Physical-system naming keeps the judgement in Phase 3 where
it can be calibrated by replay.

**Consequence.** Phase 3 composes bundles per event rather than requesting one named thing.
Slightly more work at their call site; the mapping table in section 6.2 removes most of it.

**Reversal cost.** Trivial mechanically, expensive conceptually — it would re-open D1.

---

### D4 — Named bundles are the primary selection mechanism

**Decision.** Phase 3 requests bundles. Raw per-field access exists as an escape hatch, not
as the main path.

**Alternatives rejected.**
1. *Flat field list only* — Phase 3 sends explicit field names.
2. *Bundles only*, with no raw access at all.

**Rationale against (1).** Field knowledge would leak into Phase 3: they would have to
learn a 366-field catalog, validate names, and — critically — know which five innocuous-
looking fields silently trigger a 300-credit charge (see D15). That trap would move to the
teammate least equipped to see it.

**Rationale against (2).** A single missing field would force Phase 3 to pull an entire
bundle they do not need, or to file a request against me and wait.

**Consequence.** Two call paths to maintain, and roughly 30% more surface than bundles
alone. Accepted because the escape hatch is cheap and the trap in D15 is not.

**Reversal cost.** Low. Both paths already exist; dropping one is deletion.

---

### D5 — `GET` auto-fetches on a cache miss

**Decision.** A `GET` on a bundle serves cached fields where fresh, and fetches the rest in
one Mireye call. Spending happens inside a read.

**Alternatives rejected.**
1. *Cache-only `GET` + explicit `POST :refresh` to spend* — the original recommendation.
2. *Opt-in flag* (`?fetch_if_missing=true`).

**Rationale.** Team decision: the account has credit headroom, so credit scarcity is not
the binding constraint. Under (1), Phase 3 pays two round-trips on every miss and gets a
confusingly empty result if they forget the second call — a real hazard under demo
pressure. Option (3) was judged likely to get pasted everywhere once someone hit a miss,
quietly becoming (1) anyway.

**Consequence — and this is the important one.** Every read is now potentially a spend, so
nothing in the architecture stops a runaway loop: a retry storm, a Phase 3 bug polling a
bundle, or a stray `for site in sites` during debugging. The mitigations are deliberately
*not* budget gates (see risk R2): per-(site, bundle) in-flight deduplication so concurrent
identical GETs share one fetch, a negative cache on `failed` (D8) so a broken field is not
re-fetched every request, and a loud log line when one site is fetched more than N times an
hour. `PHASE2_AUTOFETCH_ON_MISS=false` restores option (1) at runtime.

**Reversal cost.** One environment variable.

---

### D6 — Quote-first and the fetch log are retained even though there is no budget gate

**Decision.** `POST /v1/fetch/quote` runs before every fetch, and every fetch writes a
`fetch_log` row. Neither can block a request.

**Alternatives rejected.** Dropping both, since without a budget gate they gate nothing.

**Rationale.** They are not cost controls, they are *evidence*. Build Brief II grades the
architecture on credit discipline, and `GET /v1/fetch-log` is how the team demonstrates on
stage that N events cost N fetches and not N × documents. Quotes are free, unmetered, and
computed by the same code that charges, so they cannot drift from the bill. Both cost
essentially nothing to keep.

**Consequence.** One extra HTTP call per fetch. Every spend is predicted rather than
discovered, and the audit trail is complete.

**Reversal cost.** Trivial — `PHASE2_QUOTE_BEFORE_FETCH=false`. Do not, before the demo.

---

### D7 — Mireye's tri-state `status` is preserved end to end

**Decision.** Every datapoint carries `status ∈ {ok, absent, failed}` in the database and in
every API response. It is never collapsed into a nullable value.

**Alternatives rejected.** Storing `value` alone and treating `null` as "no data".

**Rationale.** The three states mean genuinely different things:

| status | meaning | bills? | cacheable? |
|---|---|---|---|
| `ok` | a real value | yes | yes, to `ttl_seconds` |
| `absent` | the source answered "nothing here" — **a real answer** | yes | yes, to `ttl_seconds` |
| `failed` | the fetch errored; `value` is null, `error`/`retryable` inline | no (refunded) | **never as an answer** |

`intersects_wetland: absent` means *there is no wetland here*, which **raises** optionality
and pushes toward alert. `intersects_wetland: failed` means *we do not know*, which should
push toward `review`. Collapsing both to `null` inverts a decision — and because HTTP is
200 in both cases, nothing else catches it.

**Consequence.** Phase 3 must handle three states. Section 8 asks them explicitly not to
collapse it.

**Reversal cost.** Not reversible without breaking correctness.

---

### D8 — `failed` is persisted as a negative cache, never as an answer

**Decision.** Failed fields are written to the datapoint table, but with `value` null,
`is_answer()` false, and expiry on a short negative-cache window (default 300 s, ×12 when
`retryable: false`) rather than on `ttl_seconds`.

**Alternatives rejected.**
1. *Do not persist failures at all* — the literal reading of "never cache a failed field".
2. *Persist failures like any other row*, expiring on `ttl_seconds`.

**Rationale.** These two requirements were in tension and the split resolves them. Option
(2) is the documented catastrophe: HTTP is 200 on a failed field, so a naive upsert freezes
the failure as truth. Option (1) is safe but means a persistently broken field is re-fetched
on *every single request*, which under D5's auto-fetch is a self-inflicted retry storm.
Storing the failure as a marker with distinct semantics gives back-off without ever letting
a failure present as a value. `retryable: false` signals a structured upstream refusal —
missing entitlement, unsupported request — where retrying cannot help, so it backs off 12×
harder.

**Consequence.** A third category in read results: `withheld` — failed rows inside their
window. They are reported to the caller, never served as values.

**Reversal cost.** Low; `PHASE2_FAILED_NEGATIVE_CACHE_S=0` approximates option (1).

---

### D9 — A failed refresh does not clobber a previously good value

**Decision.** If `slope_degrees` was `3.1` and a refresh returns `failed`, the stored value
stays `3.1` and `notes` records the failed refresh.

**Alternatives rejected.** Overwriting with the failure, as a naive upsert would.

**Rationale.** Same failure class as D7: overwriting with null reads downstream as "no
slope", which is a confident wrong answer rather than an honest gap. A stale-but-real value
with a recorded staleness is strictly more useful than a null.

**Consequence.** A value can outlive its TTL while refreshes keep failing. `stale: true` and
the `notes` string make that visible, and Phase 3 can downgrade confidence on it.

**Reversal cost.** Low, but this is a correctness guard — do not.

---

### D10 — Derived scores are computed in Phase 2, overridable by Phase 3

**Decision.** Phase 2 serves `data_center_optionality`, `bess_optionality`, and
`buildability` with default weights. Phase 3 may pass inline overrides or reference a stored
named profile.

**Alternatives rejected.**
1. *Phase 3 computes everything from raw fields* — the cleanest possible boundary.
2. *Phase 2 computes, no overrides.*

**Rationale.** The brief's central move is *"No field is called `had_data_center_optionality`
— derive it."* The banding logic (voltage classes, slope thresholds) belongs next to the
field semantics, units, and licence data that justify it; under (1) Phase 3 would have to
re-derive all of that and would be where subtle scoring bugs land. But thresholds can only
be calibrated by replay, which is Phase 3's exercise — so (2) would force a Phase 2 redeploy
every time replay suggested a tweak.

**Consequence.** Two sources of truth for what a score means. Mitigated by every derived
response echoing its `profile` name; Phase 3 must record that alongside the decision, or
"the alert says 71 and the API says 63" becomes a demo-night bug (risk R5).

**Reversal cost.** Low in either direction.

---

### D11 — Score composition is multiplicative, and that part is *not* overridable

**Decision.** Components combine multiplicatively. An override profile may change weights
and thresholds; it may not change the composition rule.

**Alternatives rejected.** Weighted additive scoring, which is the conventional choice and
is what most override schemes would naturally express.

**Rationale.** Under additive scoring, flat unpowered farmland scores as a near-miss:
terrain 1.0, clear 1.0, fiber 0.0, power 0.0 sums to roughly half marks. Phase 3 would then
alert on ground that never had the option. Multiplicatively it scores zero, which is
correct — a site with no power has no data-centre optionality no matter how flat it is.
This guard is precisely the difference between the product and the keyword feed the brief
mocks, so it is deliberately placed outside the override surface.

**Consequence.** Phase 3 cannot express an additive profile even if replay appears to favour
one. If that becomes a real need, it is a conversation, not a config change.

**Reversal cost.** Low mechanically. High in judgement — this is the single guard protecting
the demo's differentiator.

---

### D12 — Licence is resolved at write time from the runtime `source`

**Decision.** Each datapoint row stores a `license` string, derived from the `source` Mireye
actually returned, at the moment the row is written.

**Alternatives rejected.**
1. *Derive licence at read time from the field name.*
2. *Do not track licence at all* (hackathon scope).

**Rationale against (1).** `nearest_transmission_line_*` is EIA/HIFLD (public domain) while
`nearest_osm_transmission_line_*` is OpenInfraMap (ODbL). Worse, runtime provenance can be
more specific than the catalog default — the Mireye reference documents `elevation` falling
back from `USGS_EPQS` to `USGS_3DEP_COG`. A name-based rule would be wrong intermittently
and undetectably.

**Rationale against (2).** Overture Transportation/Buildings/Divisions and OSM/OpenInfraMap
fields are **ODbL — attribution plus share-alike on derived values**. Our optionality scores
*are* derived values, and Phase 3's alert is redistribution. The obligation therefore reaches
all the way into Phase 3's output, and licence can only be captured accurately at write time.

**Consequence.** Phase 3's citations should carry `source` **and** `license`, not just
`source_url`. An unrecognised source resolves to `null` rather than guessing — an unknown
licence is a thing to look up, not a thing to assume is permissive.

**Reversal cost.** Retroactively impossible for rows already written.

---

### D13 — Jurisdiction membership comes from a `boundaries` bundle, not shapefiles

**Decision.** At site registration, pull `political_region`, `political_county`,
`political_locality`, `tract_geoid` (4 credits, Census TIGER, ~1 year TTL) and serve them at
`GET /v1/sites/{id}/boundaries`.

**Alternatives rejected.** Shipping and maintaining boundary geometry (GeoJSON/shapefiles)
so Phase 3 can do point-in-polygon.

**Rationale.** Phase 3's scope resolution needs to answer "is this site inside Seattle / King
County." Four credits once per site, effectively permanent, answers it deterministically with
no geometry to source, store, version, or keep current.

**Consequence.** Works for `JURISDICTION`-scope events, which will be the majority from a
citywide Legistar feed. It does **not** help with `POLYGON`-scope events — Phase 3 still needs
real geometry for those, and Phase 1 marks unresolvable ones as unresolved rather than
guessing.

**Reversal cost.** None; additive.

---

### D14 — `/v1/proximity` is excluded entirely

**Decision.** Phase 2 never calls Mireye's proximity endpoint.

**Rationale.** It prices at 12 credits per driving calculation, and a 15-minute Los Angeles
`labor_shed` prices at ~18,792 credits. Nothing in the monitor needs drive time. Where Phase 3
needs a point-to-point `distance_m`, haversine is free and adequate.

**Consequence.** No drive-time or labour-shed capability. Nobody has asked for one.

**Reversal cost.** Low, but send `estimate: true` and `max_credits` first if it ever changes.

---

### D15 — Parcel-record fields are excluded, and a lint test is the only backstop

**Decision.** No bundle may reference any of the 19 members of Mireye's `parcel_record`
metered group. `tests/test_bundles.py` asserts this per-bundle.

**Rationale.** The group costs **300 credits per location**, charged once for *any* member.
The brief puts parcel/ownership fields out of scope. Five of the nineteen do not look like
parcel fields at all — `wetland_acres_on_parcel`, `wetland_fraction_of_parcel`,
`developable_acres_proxy`, `onsite_solar_potential_mwac_low`, `onsite_solar_potential_mwac_high`
— so membership cannot be eyeballed from a field name.

Note the near-misses that *are* safe and are needed: `wetland_acres`,
`intersects_protected_area`, `intersects_critical_habitat`, and
`intersects_conservation_easement` sit in the `parcels` **layer** but are **not** in the
metered **group**. They are 1 credit each. Confusing that the other way would make us drop
fields the scoring depends on.

**Consequence.** Since D5 removed the budget gate, this test is now the *only* thing standing
between a careless bundle edit and a 300-credit charge per call. Treat a failure as a release
blocker.

**Reversal cost.** Not reversible without accepting the cost.

---

### D16 — Mountable router, no module-level app

**Decision.** Everything hangs off a single `APIRouter`. `phase2/app.py` is a thin standalone
app for solo development and is the only file the merge may discard. No `@app.on_event`;
lifespan work lives in `init_db()`, which Phase 3 can call from their own startup.

**Rationale.** Phase 2 and Phase 3 merge later. A module-level `FastAPI()` object, or startup
hooks bound to it, would have to be unpicked by hand at merge time.

**Consequence.** Post-merge integration is one line:

```python
app.include_router(phase2.router)
```

Phase 3 talks to Phase 2 over HTTP now and in-process after merge; keeping the call surface to
the documented endpoints makes that a base-URL change, not a rewrite.

**Reversal cost.** None.

---

### D17 — All tables prefixed `p2_`

**Decision.** `p2_site`, `p2_datapoint`, `p2_fetch_log`, `p2_score_profile`, `p2_catalog_cache`.

**Rationale.** Post-merge, Phase 2 and Phase 3 may share a database. `site` and `event` are
names anyone might pick.

**Consequence.** Sharing one database file becomes a connection-string decision rather than a
schema negotiation.

**Reversal cost.** A migration. Cheap now, annoying later.

---

### D18 — SQLite, no PostGIS, no queue, no vector store

**Decision.** FastAPI + Pydantic v2 + SQLModel + SQLite + `httpx.AsyncClient`.

**Rationale.** Nothing in Phase 2 does geometry operations — D13 removed the only candidate.
Nothing needs background work. Hackathon-appropriate and matches Phase 1's Python.

**Consequence.** Concurrency ceiling is low. Irrelevant at the demo's scale.

**Reversal cost.** Low — SQLModel over Postgres is a URL change plus `check_same_thread`.

---

### D19 — Timeouts are set from the Mireye reference, not from library defaults

**Decision.** `/v1/fetch` 30 s; address-form +5.5 s; batch ≥120 s; geocode >5.5 s.

**Rationale.** A too-short client timeout **does not cancel server-side work — it keeps running
and billing.** The default 5 s in most HTTP clients is far below what these endpoints need.

**Consequence.** D5 makes this sharper: a cache miss inside a `GET` now triggers a network call,
so every bundle endpoint needs the full fetch budget or Phase 3 sees spurious failures.

**Reversal cost.** Config, but lowering these is a bug.

---

### D20 — `fetch_log.caller_ref` is opaque

**Decision.** Phase 3 may pass any string; Phase 2 stores it and never parses it.

**Rationale.** Phase 3 will want to read the credit log per canonical event at demo time.
Giving them a free-text field satisfies that without Phase 2 learning what a canonical event
is — D1 preserved at the one point where it would otherwise leak.

**Consequence.** Phase 2 cannot aggregate by event. Phase 3 can, and it is their vocabulary.

**Reversal cost.** None.

---

### D21 — The local credit estimate is advisory; `/v1/fetch/quote` is authoritative

**Decision.** `bundles.estimate_credits()` exists for sanity checks and tests. Real pricing
always comes from Mireye's quote endpoint.

**Rationale.** The quote is computed by the same code that charges, so it cannot drift from
the bill. A local estimator inevitably drifts — most obviously when a field silently joins a
metered group.

**Consequence.** `POST /v1/quote` returns both, so a divergence is visible rather than silent.

**Reversal cost.** None.

---

### D22 — As-of semantics are surfaced, not hidden

**Decision.** Every datapoint exposes `fetched_at` and a computed `stale` flag. Phase 2 makes
no attempt to reconstruct historical field values.

**Rationale.** Mireye returns fields **as-of-now, not as-of-event**. Replaying a June 2026
moratorium in August 2026 scores it against August data. For most fields this is nearly
harmless — Census TIGER and USGS terrain carry ~1 year TTLs, floodplain and wetland geometry
barely move — but **EIA Atlas (transmission, substations) carries a 1-day TTL** and genuinely
could differ.

**Consequence.** Phase 3 compares `fetched_at` against the event date and decides what to say.
The team should state the caveat in the demo rather than hide it: the entire product claim is
about dates, and being caught fudging one costs far more than the caveat does. A judge who
hears "these are current-state fields and here is the exposure" trusts the lead-time number
*more*, not less.

**Reversal cost.** Not reversible — Mireye has no time-travel API.

---

## 4. Data model

All tables prefixed `p2_` (D17). SQLite (D18).

### `p2_site`

```text
id                  uuid PK
label               text
address_raw         text
lat, lng            float          -- authoritative
geocode_accuracy    float          -- `accuracy_type` matters MORE than this
accuracy_type       text
match_type          text
normalized_address  text
geocode_provider    text
parcel_grade        bool           -- FALSE => coordinate may be a neighbour's parcel
precision_note      text
geocoded_at         timestamptz
political_region    text           -- D13, pulled once at registration
political_county    text
political_locality  text
tract_geoid         text
created_at          timestamptz
```

`site.degraded` is `parcel_grade is False`. Every datapoint served for such a site is marked
degraded; see risk R4.

### `p2_datapoint`

One row per `(site_id, field_name)`, unique. Mirrors Mireye's per-field record exactly.

```text
id, site_id, field_name
value              json    -- null unless status == 'ok'
unit               text
status             text    -- 'ok' | 'absent' | 'failed'   (D7)
error, retryable           -- populated only when failed   (D8)
source, source_url
license            text    -- resolved at write time from runtime source (D12)
confidence         text    -- 'high' | 'medium' | 'low' | 'unknown'
fetched_at         timestamptz   -- authoritative as-of, NOT created_at (D22)
dataset_vintage, ttl_seconds, notes
request_id         text    -- X-Request-ID, for support correlation
created_at, updated_at
```

### `p2_fetch_log`

```text
id, site_id, fields[]
quoted_credits, charged_credits, credits_remaining
request_id, idempotency_key
trigger      -- 'registration' | 'cache_miss' | 'refresh' | 'replay'
caller_ref   -- opaque, D20
ok, error, created_at
```

### `p2_score_profile`

```text
id, metric, name, weights json, thresholds json, created_at
UNIQUE (metric, name)
```

### `p2_catalog_cache`

Mirror of `GET /v1/meta/fields` (public, ETag-cached, 1 h TTL). Validates field names locally
so `400 fields_unknown` never reaches production and a typo in a bundle definition surfaces
at startup rather than mid-demo.

---

## 5. API surface

### Implemented

| Method | Path | Notes |
|---|---|---|
| `GET` | `/v1/healthz` | token configured? autofetch on? |
| `GET` | `/v1/bundles` | definitions, local estimates, parcel-trap check |
| `GET` | `/v1/catalog/fields` | mirrored catalog, ETag-aware |
| `POST` | `/v1/quote` | free, unmetered, exact; accepts bundles and/or fields |
| `GET` | `/v1/budget` | informational only — there is no budget gate (D5) |

### Planned (steps 4–9)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/sites` | register; geocodes once, pulls `boundaries` |
| `GET` | `/v1/sites/{id}` | detail incl. geocode provenance |
| `GET` | `/v1/sites/{id}/{bundle}` | **primary path** — auto-fetches on miss (D5) |
| `GET` | `/v1/sites/{id}/datapoints[/{field}]` | escape hatch (D4) |
| `POST` | `/v1/sites/{id}/datapoints:refresh` | force refetch even when fresh |
| `GET` | `/v1/sites/{id}/derived/{metric}` | `?profile=` supported (D10) |
| `POST` | `/v1/sites/{id}/derived/{metric}` | inline weight/threshold override |
| `GET`/`POST` | `/v1/score-profiles` | list / store named profiles |
| `GET` | `/v1/fetch-log` | credit audit trail (D6) |

Bundle responses carry a cache block so cost is visible on every read:

```json
{
  "bundle": "grid",
  "cache": {"hits": 4, "fetched": 2, "credits_spent": 2, "quoted": 2},
  "datapoints": []
}
```

---

## 6. Bundle catalog

### 6.1 Definitions

| Bundle | Credits | Fields |
|---|---|---|
| `grid` | 6 | `nearest_transmission_line_voltage_kv`, `nearest_transmission_line_distance_m`, `nearest_substation_distance_m`, `max_transmission_line_voltage_kv_within_radius`, `transmission_redundancy_flag`, `interconnection_queue_active_capacity_county_mw` |
| `telecom` | 3 | `fiber_broadband_available`, `fiber_provider_count`, `mobile_5g_coverage_class` |
| `terrain` | 3 | `slope_degrees`, `elevation`, `grading_difficulty_class` |
| `water` | 4 | `within_floodplain_polygon`, `fema_flood_zone`, `intersects_wetland`, `wetland_acres` |
| `constraints` | 3 | `intersects_protected_area`, `intersects_critical_habitat`, `intersects_conservation_easement` |
| `access` | 3 | `nearest_major_road_distance_m`, `nearest_major_road_class`, `nearest_rail_line_distance_m` |
| `boundaries` | 4 | `political_region`, `political_county`, `political_locality`, `tract_geoid` |

All bundle combinations stay under Mireye's **50-field fetch cap**. Note that the
`data_center_siting` preset (135 fields) and `site_selection` (72) **cannot be fetched whole**
— presets are exempt from the explicit-field cap but the resolved set still caps at 50. We
name fields; we never send a large preset.

### 6.2 Recommended event → bundle mapping

**Phase 3 owns this table.** It is published here so they need not read 366 field names. The
mapping encodes a materiality judgement, which is why it lives on their side (D1, D3).

| Event type | Subject | Bundles | Credits |
|---|---|---|---|
| moratorium | data center | grid + telecom + terrain + water + constraints | 19 |
| moratorium | BESS / battery | grid + terrain + water + constraints | 16 |
| moratorium | other / unknown | terrain + water + constraints + access | 13 |
| rezoning | any | terrain + water + constraints + access | 13 |
| annexation | any | terrain + water + constraints + access | 13 |
| comp-plan amendment | any | terrain + water + constraints + access | 13 |
| utility extension | sewer / water | terrain + water + access | 10 |
| utility extension | power | grid + terrain + access | 12 |
| major development permit | any | terrain + water + constraints + access | 13 |

---

## 7. Contracts

### 7.1 Typical Phase 3 call sequence

```text
1. Phase 3 receives a canonical event from Phase 1
2. Phase 3 resolves scope        (its job: event geometry x site -> relation, distance_m)
3. Phase 3 picks bundles          (its mapping, 6.2)
4. GET /v1/sites/{id}/{bundle}    -- cache or fetch, one call
5. GET /v1/sites/{id}/derived/{metric}[?profile=...]
6. Phase 3 combines -> POST /v1/decide -> alert | review | quiet
```

### 7.2 What Phase 2 will **not** give you

- `relation_to_site` or `distance_m` — we do not know where the event is (D1)
- A materiality score, impact direction, or stage language
- Inference across sites; every request names exactly one site
- Any endpoint taking an `event_type` parameter

---

## 8. Open cross-phase asks

**→ Phase 1: keep `subject` in the emitted event.** Your Step 5 LLM schema captures it; your
Step 10 interface object drops it. Without it Phase 3 cannot tell a data-centre moratorium
from a BESS one, and `fiber_broadband_available` is decisive for the first and pure noise for
the second (6.2). One-line fix in the interface object.

**→ Phase 1 + Phase 3: reconcile the stage vocabulary.** Phase 1 emits six stages
(`PROPOSED HEARD ADOPTED REJECTED WITHDRAWN TABLED`); Phase 3 accepts four
(`proposed heard adopted unknown`). `REJECTED` is not `unknown` — a rejected data-centre
moratorium **restores** option value and is a legitimate positive-direction alert.
`WITHDRAWN`/`TABLED` are closer to non-events. Mapping all three to `unknown` sends them to
`review` and pollutes the precision number the demo is built on.

**→ Phase 1 + Phase 3: agree the confidence type.** Phase 1 emits a float (`0.96`); Mireye
emits `high|medium|low|unknown`; Phase 3's contract expects a string. Phase 2 owns the Mireye
side and normalises to the string enum. Someone must own the Phase 1 float → string mapping.

**→ Phase 3: send the same site for every stage of one event.** Physical facts do not change
across a bill's lifecycle. Reuse of the TTL cache is automatic (D2) as long as the site is the
same — no event id needed.

**→ Phase 3: do not collapse `absent` into missing.** See D7. This one can invert a decision.

**→ Phase 3: carry `license` into citations**, not just `source_url`. See D12.

**→ Phase 3: record the `profile` name** returned with every derived score. See D10, R5.

---

## 9. Risk register

**R1 — Seattle may not discriminate physically. Highest severity; lands on Phase 2.**
Phase 3's winning story is *"we stayed quiet on non-buildable ground."* That requires
monitored sites whose physical fields actually diverge. Seattle city limits is dense and
urban: near-universal fiber, short substation distances, little wetland, almost nothing
protected. If every site scores high optionality, Phase 3 never emits a `quiet` and the one
thing separating this product from a keyword feed never appears on stage. Seattle does have
real variation — Duwamish floodplain, West Seattle and Magnolia steep slopes — so this is
testable rather than fatal. **Mitigation:** build step 7 registers 8 deliberately spread
Seattle coordinates and pulls the optionality bundles, ~150 credits, one afternoon. It is
scheduled early precisely because it is the only step that can invalidate the demo premise.
If the scores do not separate, add King County for rural ground with genuine
transmission-versus-terrain tension; Build Brief II permits "a county, a town, or a single
address", so widening is not a scope violation.

**R2 — Auto-fetch with no budget gate has no backstop against a runaway loop.** Consequence
of D5. Mitigations are in-flight deduplication, the D8 negative cache, and a loud log on
repeated fetches of one site — deliberately none of them a budget gate.

**R3 — Gated catalog fields.** Some recent `climate`/`hazards`/`utilities`/`parcels` additions
are catalog-listed but not yet ingested; they return honest provenance nulls with an
explanatory `notes` rather than fabricated values. Every field in 6.1 must be verified against
a real Seattle coordinate before demo day.

**R4 — The `parcel_grade: false` trap.** A geocode landing on a neighbour's parcel produces a
confidently wrong slope or floodplain answer with no symptom. Flagged at registration, every
datapoint for that site marked degraded; Phase 3 should route those to `review`.

**R5 — Score-profile drift.** With D10 overrides live, the alert text and the API can disagree
about a score. Every derived response echoes `profile`; Phase 3 must record it with the
decision.

**R6 — `credits_exhausted` (402).** A hard stop, not overage billing — the request is simply
not served. Must degrade gracefully mid-demo: serve the cached snapshot, mark it stale, say so.

**R7 — Boundary erosion.** If an `?event_type=` parameter appears on a Phase 2 endpoint, D1 has
leaked and the D6 credit story stops being provable.

---

## 10. Progress

| Step | State | Credits |
|---|---|---|
| 0 · Scaffold, `APIRouter` layout, config | **done** | 0 |
| 1 · Catalog mirror, `/v1/quote`, `/v1/budget` | **done** | 0 |
| 2 · Datapoint store: tri-state, TTL, never-cache-a-failure | **done** | 0 |
| 3 · Bundle definitions + parcel-trap lint | **done** | 0 |
| 4 · Site registry + geocode + `boundaries` | blocked on `MIREYE_API_TOKEN` for live calls | 5/site |
| 5 · Fetch orchestrator (quote → fetch → normalize → store → log) | next | ~19 |
| 6 · Bundle endpoints with cache-miss auto-fetch + in-flight dedup | next | 0 |
| 7 · **Seattle spread test, 8 coordinates** | de-risks R1 | ~150 |
| 8 · Derived scores + profile override | pending | 0 |
| 9 · Raw field escape hatch + `/v1/fetch-log` | pending | 0 |

Steps 5 and 6 are the ones Phase 3 actually calls. They are buildable and testable against
mocked transports; the token is only required for live calls and for step 7.

**Test suite:** 50 tests. 15 cover the parcel trap and bundle shape; 17 cover tri-state
status, TTL freshness, negative caching, and provenance. Run:
`.venv/Scripts/python.exe -m pytest tests -q`

**Run locally:** `.venv/Scripts/python.exe -m uvicorn phase2.app:app --reload --port 8002`

---

## 11. Configuration

| Variable | Default | Effect |
|---|---|---|
| `MIREYE_API_TOKEN` | — | Dashboard API token. **MCP OAuth tokens are not accepted on `/v1/*`** |
| `MIREYE_BASE_URL` | `https://api.mireye.com` | |
| `PHASE2_DATABASE_URL` | `sqlite:///./phase2.db` | |
| `PHASE2_AUTOFETCH_ON_MISS` | `true` | `false` restores explicit-refresh mode (D5) |
| `PHASE2_QUOTE_BEFORE_FETCH` | `true` | Leave on — it is demo evidence (D6) |
| `PHASE2_FAILED_NEGATIVE_CACHE_S` | `300` | Negative-cache window; ×12 when `retryable: false` (D8) |
| `PHASE2_CATALOG_TTL_S` | `3600` | |
| `PHASE2_FETCH_WARN_PER_SITE_PER_HOUR` | `20` | Runaway-loop log threshold (R2) |
