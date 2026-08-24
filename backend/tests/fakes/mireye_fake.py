"""A fake Mireye Earth API, wired in as an `httpx.MockTransport`.

No real `MIREYE_API_TOKEN` or network access is available in this environment
(`scripts/probe_legistar.py`'s own header notes the sandbox has no network access to
government APIs either -- the same constraint applies to Mireye). This fake lets every
real Phase 2 code path run for real -- quote, fetch, store, orchestrate, score -- with
only the actual HTTP boundary faked, exactly the way Phase 1 already fakes the Legistar
HTTP boundary with `FakeSource` in its own test suite.

Two named coordinate profiles are hand-authored to be physically discriminating on
purpose -- this directly answers the risk flagged in context/phase2.md ("Seattle may not
discriminate physically"): a dense-city sample alone risks every site scoring similar
optionality, which would mean Phase 3 could never legitimately go SILENCE on physical
grounds. GOOD_SITE and BAD_SITE are constructed to land on opposite sides of the
data_center_optionality threshold so the demo's SILENCE case is a real, motivated outcome
of the scoring math, not a coin flip.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

from phase2.bundles import PARCEL_RECORD_GROUP, estimate_credits, fields_for

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()

# SODO-industrial-shaped: strong grid, fiber, flat, clear of every constraint flag.
GOOD_SITE = (47.5480, -122.3300)
# Duwamish-floodplain-shaped: weak/distant power, no fiber, flagged on every water/
# constraint field. Deliberately the physical opposite of GOOD_SITE.
BAD_SITE = (47.5310, -122.3554)

_ADDRESSES = {
    "1000 1st Ave S, Seattle, WA": GOOD_SITE,
    "800 S Michigan St, Seattle, WA": BAD_SITE,
}


def _rec(value: Any, *, status: str = "ok", source: str = "EIA Energy Atlas", **kw) -> dict:
    row = {
        "value": value,
        "unit": kw.pop("unit", None),
        "status": status,
        "error": kw.pop("error", None),
        "retryable": kw.pop("retryable", None),
        "source": source,
        "source_url": kw.pop("source_url", f"https://example.invalid/{source}"),
        "confidence": kw.pop("confidence", "high"),
        "fetched_at": NOW_ISO,
        "dataset_vintage": kw.pop("dataset_vintage", "2026-06"),
        "ttl_seconds": kw.pop("ttl_seconds", 86400),
        "notes": kw.pop("notes", None),
    }
    row.update(kw)
    return row


def _good_fields() -> dict[str, dict]:
    return {
        "nearest_transmission_line_voltage_kv": _rec(230, unit="kV"),
        "nearest_transmission_line_distance_m": _rec(1200, unit="m"),
        "nearest_substation_distance_m": _rec(3400, unit="m"),
        "max_transmission_line_voltage_kv_within_radius": _rec(230, unit="kV"),
        "transmission_redundancy_flag": _rec(True),
        "interconnection_queue_active_capacity_county_mw": _rec(450, unit="MW"),
        "fiber_broadband_available": _rec(True, source="Overture Places"),
        "fiber_provider_count": _rec(4),
        "mobile_5g_coverage_class": _rec("full"),
        "slope_degrees": _rec(2.1, unit="deg", source="USGS 3DEP"),
        "elevation": _rec(15, unit="m", source="USGS 3DEP"),
        "grading_difficulty_class": _rec("easy"),
        "within_floodplain_polygon": _rec(None, status="absent", source="FEMA"),
        "fema_flood_zone": _rec(None, status="absent", source="FEMA"),
        "intersects_wetland": _rec(None, status="absent", source="USGS NHD"),
        "wetland_acres": _rec(None, status="absent", source="USGS NHD"),
        "intersects_protected_area": _rec(None, status="absent", source="USGS PAD-US"),
        "intersects_critical_habitat": _rec(None, status="absent", source="USFWS"),
        "intersects_conservation_easement": _rec(None, status="absent", source="NCED"),
        "nearest_major_road_distance_m": _rec(150, unit="m", source="OpenStreetMap"),
        "nearest_major_road_class": _rec("arterial", source="OpenStreetMap"),
        "nearest_rail_line_distance_m": _rec(800, unit="m", source="OpenStreetMap"),
        "political_region": _rec("Washington", source="Census"),
        "political_county": _rec("King County", source="Census"),
        "political_locality": _rec("Seattle", source="Census"),
        "tract_geoid": _rec("53033008500", source="Census"),
    }


def _bad_fields() -> dict[str, dict]:
    return {
        "nearest_transmission_line_voltage_kv": _rec(12, unit="kV"),
        "nearest_transmission_line_distance_m": _rec(9500, unit="m"),
        "nearest_substation_distance_m": _rec(32000, unit="m"),
        "max_transmission_line_voltage_kv_within_radius": _rec(12, unit="kV"),
        "transmission_redundancy_flag": _rec(False),
        "interconnection_queue_active_capacity_county_mw": _rec(5, unit="MW"),
        "fiber_broadband_available": _rec(False, source="Overture Places"),
        "fiber_provider_count": _rec(0),
        "mobile_5g_coverage_class": _rec("partial"),
        "slope_degrees": _rec(1.0, unit="deg", source="USGS 3DEP"),
        "elevation": _rec(4, unit="m", source="USGS 3DEP"),
        "grading_difficulty_class": _rec("moderate"),
        "within_floodplain_polygon": _rec(True, source="FEMA"),
        "fema_flood_zone": _rec("AE", source="FEMA"),
        "intersects_wetland": _rec(True, source="USGS NHD"),
        "wetland_acres": _rec(3.2, unit="ac", source="USGS NHD"),
        "intersects_protected_area": _rec(True, source="USGS PAD-US"),
        "intersects_critical_habitat": _rec(True, source="USFWS"),
        "intersects_conservation_easement": _rec(None, status="absent", source="NCED"),
        "nearest_major_road_distance_m": _rec(2200, unit="m", source="OpenStreetMap"),
        "nearest_major_road_class": _rec("local", source="OpenStreetMap"),
        "nearest_rail_line_distance_m": _rec(6000, unit="m", source="OpenStreetMap"),
        "political_region": _rec("Washington", source="Census"),
        "political_county": _rec("King County", source="Census"),
        "political_locality": _rec("Seattle", source="Census"),
        "tract_geoid": _rec("53033009400", source="Census"),
    }


def _generic_fields() -> dict[str, dict]:
    """Any coordinate that isn't one of the two curated profiles -- a plausible-looking
    but deliberately mediocre response so arbitrary demo coordinates don't crash, without
    quietly forcing every unknown site toward ALERT or SILENCE."""
    return {
        "nearest_transmission_line_voltage_kv": _rec(115, unit="kV"),
        "nearest_transmission_line_distance_m": _rec(5000, unit="m"),
        "nearest_substation_distance_m": _rec(12000, unit="m"),
        "max_transmission_line_voltage_kv_within_radius": _rec(115, unit="kV"),
        "transmission_redundancy_flag": _rec(False),
        "interconnection_queue_active_capacity_county_mw": _rec(50, unit="MW"),
        "fiber_broadband_available": _rec(True, source="Overture Places"),
        "fiber_provider_count": _rec(1),
        "mobile_5g_coverage_class": _rec("partial"),
        "slope_degrees": _rec(6.0, unit="deg", source="USGS 3DEP"),
        "elevation": _rec(30, unit="m", source="USGS 3DEP"),
        "grading_difficulty_class": _rec("moderate"),
        "within_floodplain_polygon": _rec(None, status="absent", source="FEMA"),
        "fema_flood_zone": _rec(None, status="absent", source="FEMA"),
        "intersects_wetland": _rec(None, status="absent", source="USGS NHD"),
        "wetland_acres": _rec(None, status="absent", source="USGS NHD"),
        "intersects_protected_area": _rec(None, status="absent", source="USGS PAD-US"),
        "intersects_critical_habitat": _rec(None, status="absent", source="USFWS"),
        "intersects_conservation_easement": _rec(None, status="absent", source="NCED"),
        "nearest_major_road_distance_m": _rec(600, unit="m", source="OpenStreetMap"),
        "nearest_major_road_class": _rec("collector", source="OpenStreetMap"),
        "nearest_rail_line_distance_m": _rec(3000, unit="m", source="OpenStreetMap"),
        "political_region": _rec("Washington", source="Census"),
        "political_county": _rec("King County", source="Census"),
        "political_locality": _rec("Seattle", source="Census"),
        "tract_geoid": _rec("53033009900", source="Census"),
    }


def _profile_for(lat: float | None, lng: float | None) -> dict[str, dict]:
    if lat is None or lng is None:
        return _generic_fields()
    if (round(lat, 3), round(lng, 3)) == (round(GOOD_SITE[0], 3), round(GOOD_SITE[1], 3)):
        return _good_fields()
    if (round(lat, 3), round(lng, 3)) == (round(BAD_SITE[0], 3), round(BAD_SITE[1], 3)):
        return _bad_fields()
    return _generic_fields()


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    body: dict[str, Any] = json.loads(request.content or b"{}") if request.content else {}

    if path == "/v1/fetch/quote" and request.method == "POST":
        fields = body.get("fields") or []
        locations = body.get("locations", 1)
        per_loc = estimate_credits(fields)
        return httpx.Response(
            200,
            json={
                "credits_per_location": per_loc,
                "credits_total": per_loc * locations,
                "breakdown": {f: (300 if f in PARCEL_RECORD_GROUP else 1) for f in fields},
                "allowance": {
                    "credits_included": 100_000,
                    "credits_used": 1_250,
                    "credits_remaining": 98_750,
                    "would_exceed_allowance": False,
                    "would_be_blocked": False,
                },
                "notes": None,
            },
        )

    if path == "/v1/fetch" and request.method == "POST":
        fields = body.get("fields") or []
        profile = _profile_for(body.get("lat"), body.get("lng"))
        response_fields = {f: profile[f] for f in fields if f in profile}
        return httpx.Response(
            200,
            json={
                "fields": response_fields,
                "resolved_location": {"lat": body.get("lat"), "lng": body.get("lng"), "source": "input"},
                "geocode": None,
                "partial_failures": [],
                "notes": None,
                "data_gaps": None,
            },
        )

    if path == "/v1/geocode" and request.method == "POST":
        address = body.get("address")
        lat, lng = _ADDRESSES.get(address, GOOD_SITE)
        return httpx.Response(
            200,
            json={
                "lat": lat,
                "lng": lng,
                "accuracy": 0.95,
                "accuracy_type": "rooftop",
                "match_type": "exact",
                "normalized_address": address,
                "provider": "fake-geocoder",
                "source": "fake-geocoder",
                "parcel_grade": True,
                "precision_note": None,
            },
        )

    if path == "/v1/meta/fields" and request.method == "GET":
        names = sorted(fields_for(["grid", "telecom", "terrain", "water", "constraints", "access", "boundaries"]))
        return httpx.Response(200, json={"fields": names}, headers={"ETag": '"fake-etag-1"'})

    if path == "/v1/users/me/usage" and request.method == "GET":
        return httpx.Response(
            200,
            json={"credits_included": 100_000, "credits_used": 1_250, "credits_remaining": 98_750},
        )

    return httpx.Response(404, json={"detail": {"error": "not_found", "message": path, "retryable": False}})


def make_transport() -> httpx.MockTransport:
    return httpx.MockTransport(handler)
