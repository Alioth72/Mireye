# Bug report — `protected_area_*` returns Papahanaumokuakea for all coordinates in a latitude band

**Reported by:** Mireye Earth API user (Build plan)
**Date observed:** 2026-08-24
**Catalog version:** 0.16.0 (`/v1/meta/fields` reports 327 fields)
**Severity:** high — silently corrupts a documented field across a large part of the continental US

---

## Summary

Every coordinate whose latitude falls between roughly **25.3°N and 31.6°N** returns

```
intersects_protected_area  = True
protected_area_name        = Papahanaumokuakea Marine National Monument
protected_area_gap_status  = 2
protected_area_manager     = FWS
protected_area_designation = NM
```

**regardless of longitude.** Papahanaumokuakea is a marine national monument in the Pacific
Ocean northwest of Hawai'i (roughly 22.75–29.5°N, **161–180°W**). It is being reported for
inland coordinates in Texas, Florida, and northern Mexico.

Outside that latitude band the field behaves correctly.

**Scope of the defect is narrow and worth stating precisely.** Location resolution is not
affected, and no other field is affected. In the same response for San Antonio City Hall,
every other value is correct for Texas — `iso_rto: ERCOT`, `egrid_subregion: ERCT`,
`elevation: 196.6 m`, `coast_distance_m: 168,939`, `design_wet_bulb: 25.2 °C`,
`days_above_32c: 121`, `nearest_transmission_line_voltage_kv: 138`. Only the
`protected_area_*` group is wrong. A single point-in-polygon match is returning the wrong
row; nothing is being mis-located.

---

## Reproduction — using addresses only, no caller-supplied coordinates

This form is given deliberately: the API performs its own geocoding, so no coordinate of
ours enters the request. The API also confirms the resolved locality in the same response.

```
POST /v1/geocode  {"address": "100 Military Plaza, San Antonio, TX 78205"}
  -> lat 29.4246, lng -98.4951

POST /v1/fetch    {"lat": 29.4246, "lng": -98.4951,
                   "fields": ["protected_area_name", "intersects_protected_area",
                              "political_locality", "political_region"]}
  -> political_locality      "San Antonio"
     political_region        "Texas"
     intersects_protected_area  true
     protected_area_name     "Papahanaumokuakea Marine National Monument"
```

Same result for `400 Biscayne Blvd, Miami, FL 33132` (geocodes to 25.7784, -80.1889;
returns `political_locality: "Miami"`, and the same monument).

**Control, outside the band:** `233 S Wacker Dr, Chicago, IL 60606` (41.8787, -87.6359)
returns `protected_area_name: null`, `intersects_protected_area: false`. Correct.

---

## Band boundary

Latitude sweep at fixed longitude `-98.4936`, 0.625° steps:

| lat | `intersects_protected_area` | `protected_area_name` |
|---|---|---|
| 25.000 | false | — |
| **25.625** | **true** | **Papahanaumokuakea** |
| 26.250 – 30.625 | true | Papahanaumokuakea (every step) |
| **31.250** | **true** | **Papahanaumokuakea** |
| 31.875 | false | — |
| 32.500 – 35.000 | false | — |

Band edges therefore lie between 25.000–25.625°N and 31.250–31.875°N.

---

## Longitude has no effect

Fixed latitude **28.5°N**, longitude swept across the continental US:

| lng | `political_region` | `protected_area_name` |
|---|---|---|
| -120.0 | (ocean) | Papahanaumokuakea |
| -115.0 | (ocean) | Papahanaumokuakea |
| -110.0 | Sonora, MX | Papahanaumokuakea |
| -105.0 | Chihuahua, MX | Papahanaumokuakea |
| -100.0 | Texas | Papahanaumokuakea |
| -95.0 | (Gulf) | Papahanaumokuakea |
| -90.0 | (Gulf) | Papahanaumokuakea |
| -85.0 | (Gulf) | Papahanaumokuakea |
| -81.0 | Florida | Papahanaumokuakea |

9 of 9. The match is a function of latitude alone.

---

## Incidence

| Sample | Points | Intersecting a protected area | Of those, Papahanaumokuakea |
|---|---|---|---|
| Random, lat 31.0–47.5°N, lng -121 to -76 | 25 | 5 | **0** |
| Random, lat 25.5–30.5°N, lng -106 to -80 | 25 | 25 | **25** |

The 5 correct hits in the northern sample were Gold Butte National Monument (NV),
Humboldt River Field Office (NV), Proposed Open Space (AZ), Gunnison National Forest (CO),
and Lander Field Office (WY) — all plausible for their coordinates.

In the southern sample, points located in **Durango, Chihuahua and Nuevo León, Mexico**,
and points in open water, also returned the monument.

---

## Why this is not a caller-side error

1. `resolved_location` echoes the submitted coordinate exactly, with `source: "coordinate"`.
   No geocoding drift.
2. The same response places the point correctly via `political_region` / `political_county`
   (`Texas` / `Bexar County`). The service knows where the point is.
3. The address-based reproduction above supplies no coordinates at all.
4. The field is documented as *"Name of the **intersecting** PAD-US protected area (most
   protective unit)"* — so this is not nearest-neighbour behaviour. Hawai'i would not be
   the nearest match to Texas in any case.
5. Fresh requests, no caching involved; reproduces across separate requests, coordinates
   and input forms.

---

## Hypothesis (offered tentatively — we cannot distinguish these from outside)

The observed behaviour is consistent with the Papahanaumokuakea polygon having lost or
wrapped its **longitude** bound, leaving a latitude-only band that matches globally. Its
real latitude extent (~22.75–29.5°N) sits close to the observed band (~25.3–31.6°N).

Whether the defect originates in the PAD-US join or in the USGS PAD-US 4.1 source data is
not something we can determine from the API surface.

---

## Impact

`intersects_protected_area` and `protected_area_gap_status` are commonly used as
development-constraint signals. A GAP-2 designation reads as real conservation protection,
so any consumer weighting it will heavily penalise every site in the band.

The band covers all of Florida, most of Texas, Louisiana, Mississippi, Alabama, southern
Georgia, and southern Arizona and New Mexico — including the Dallas, Atlanta, Phoenix and
San Antonio metros, several of which are significant data-centre markets.

The failure is silent: `status` is `ok`, `confidence` is normal, and nothing in the
response indicates a problem. A consumer has no signal to distrust the value.

---

## Suggested check

Query `protected_area_name` at any inland coordinate between 26°N and 31°N. If it returns
Papahanaumokuakea, the polygon's longitude bound is the place to look.
