# Bug report — `protected_area_*` matches a Pacific monument across a US latitude band

**Date observed:** 2026-08-24 · **Catalog:** 0.16.0 · **Plan:** Build
**Severity:** high — silently corrupts a documented field across the US Gulf coast

---

## Summary

Every coordinate between roughly **25.3°N and 31.6°N**, at **any longitude the API
accepts**, returns:

```
intersects_protected_area  = true
protected_area_name        = Papahanaumokuakea Marine National Monument
protected_area_gap_status  = 2
protected_area_manager     = FWS
```

Papahanaumokuakea is a **marine** monument in the Pacific northwest of Hawai'i
(~22.75–29.5°N, **161–180°W**). It is being reported as the *intersecting* protected area
for inland cities in Texas, Louisiana, Alabama and Florida.

**Only the `protected_area_*` group is wrong.** Location resolution is correct and no
other field is affected — see "Not caller-side" below.

---

## Evidence: 12 cities, with distance to the monument

| City | lat | Result | GAP | mi to centre | mi to nearest edge |
|---|---|---|---|---|---|
| Miami FL | 25.78 | **Papahanaumokuakea** | 2 | 5,553 | 5,056 |
| Corpus Christi TX | 27.80 | **Papahanaumokuakea** | 2 | 4,512 | 3,994 |
| Tampa FL | 27.95 | **Papahanaumokuakea** | 2 | 5,366 | 4,878 |
| San Antonio TX | 29.42 | **Papahanaumokuakea** | 2 | 4,416 | 3,908 |
| Houston TX | 29.76 | **Papahanaumokuakea** | 2 | 4,589 | 4,089 |
| New Orleans LA | 29.95 | **Papahanaumokuakea** | 2 | 4,886 | 4,398 |
| Jacksonville FL | 30.33 | **Papahanaumokuakea** | 2 | 5,347 | 4,878 |
| Baton Rouge LA | 30.45 | **Papahanaumokuakea** | 2 | 4,812 | 4,325 |
| Mobile AL | 30.70 | **Papahanaumokuakea** | 2 | 4,983 | 4,504 |
| El Paso TX | 31.76 | correct (`false` / `null`) | — | 3,916 | 3,417 |
| Savannah GA | 32.08 | correct (`false` / `null`) | — | 5,331 | 4,877 |
| Tucson AZ | 32.22 | correct (`false` / `null`) | — | 3,651 | 3,153 |

**Distance does not predict the result; latitude predicts it perfectly.**

* **Tucson (3,651 mi) and El Paso (3,916 mi) are the two closest cities to the monument,
  and both are clean.** Miami, the furthest at 5,553 mi, is flagged. Nearest-neighbour
  behaviour would produce the opposite.
* **Savannah (5,331 mi, clean) vs Jacksonville (5,347 mi, flagged)** — 16 miles apart in
  distance from the monument, opposite results. Savannah is also *east* of Jacksonville.
  The only meaningful difference is 1.7° of latitude.

Every affected city is 3,900–5,100 miles from the monument's **nearest** edge.

---

## Reproduction — addresses only, no caller-supplied coordinates

```
POST /v1/geocode  {"address": "100 Military Plaza, San Antonio, TX 78205"}
  -> 29.4246, -98.4951

POST /v1/fetch    {"lat": 29.4246, "lng": -98.4951,
                   "fields": ["intersects_protected_area", "protected_area_name",
                              "political_region"]}
  -> political_region           "Texas"
     intersects_protected_area  true
     protected_area_name        "Papahanaumokuakea Marine National Monument"
```

Same for `400 Biscayne Blvd, Miami, FL 33132`.
**Control:** `233 S Wacker Dr, Chicago, IL 60606` (41.88°N) returns `false` / `null`.

---

## Band boundary and longitude independence

Latitude sweep at fixed lng `-98.4936`: clean at 25.000, affected from **25.625 through
31.250**, clean again at 31.875. Combined with El Paso (31.76, clean), the north edge sits
between **31.25°N and 31.76°N**.

Longitude sweep at fixed lat 28.5°N: `-120, -115, -110, -105, -100, -95, -90, -85, -81`
— **9 of 9 return the monument**, spanning Pacific Ocean, Sonora, Chihuahua, Texas, Gulf of
Mexico and Florida.

Random sampling: **0 of 5** protected-area hits in a 31–47°N sample were Papahanaumokuakea
(correct hits: Gold Butte NM, Gunnison NF, Lander Field Office, Humboldt River Field
Office). **25 of 25** in a 25.5–30.5°N sample were, including points in Durango, Chihuahua
and Nuevo León, Mexico, and open water.

*Extent beyond US longitudes is untestable* — `/v1/fetch` rejects `lng` outside −180 to −65
with `400 coord_out_of_bounds`, so a same-latitude control such as Cairo cannot be run.

---

## Not caller-side

1. `resolved_location` echoes the submitted coordinate with `source: "coordinate"`.
2. The **same response** reports the location correctly: for San Antonio City Hall,
   `political_region: Texas`, `political_county: Bexar County`, `iso_rto: ERCOT`,
   `egrid_subregion: ERCT`, `elevation: 196.6 m`, `coast_distance_m: 168,939`. Hawai'i is
   in no ISO/RTO, and a marine monument is not 197 m above sea level 105 miles inland.
3. The reproduction above supplies **no coordinates at all** — the API geocodes.
4. The field is documented as *"Name of the **intersecting** PAD-US protected area"* — not
   nearest. And the distance table shows it is not behaving as nearest either.

---

## Hypothesis

Consistent with the Papahanaumokuakea polygon having lost or wrapped its **longitude**
bound, leaving a latitude-only stripe. Its real latitude extent (~22.75–29.5°N) sits close
to the observed band (~25.3–31.6°N).

Whether this originates in the PAD-US join or in USGS PAD-US 4.1 source data cannot be
determined from the API surface.

---

## Impact

`intersects_protected_area` and `protected_area_gap_status` are standard
development-constraint signals, and GAP 2 reads as real conservation protection. The band
covers Florida, most of Texas, Louisiana, Mississippi, Alabama, southern Georgia and
southern Arizona/New Mexico — including the Dallas, Atlanta, Phoenix, Houston and San
Antonio metros.

**The failure is silent:** `status: ok`, normal `confidence`, nothing anomalous in the
response. A consumer has no signal to distrust the value.

**Suggested check:** query `protected_area_name` at any inland coordinate between 26°N and
31°N. If it returns Papahanaumokuakea, the polygon's longitude bound is where to look.
