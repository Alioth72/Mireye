"""Scope resolution: does this event's geography actually cover this monitored site?

Explicitly Phase 3's job, not Phase 2's -- context/phase2.md: "Scope resolution (event
geography x site coordinate -> relation) belongs to Phase 3. It determines whether and
where to fetch, which makes it a decision, not a lookup." Phase 2 supplies the raw
ingredients (a site's `political_region`/`political_locality`, pulled once via the
`boundaries` bundle at registration); this module does the comparison.

Never guesses relevance -- the same principle Phase 1's own geo.py applies to avoid
hallucinating geometry, and Phase 2's store.py applies to avoid treating a failed fetch as
an answer. An UNRESOLVED or ambiguous geography always resolves to SILENCE here, never to
a best-effort ALERT.
"""

from __future__ import annotations

import math

# Phase 1's geo.py only ever emits POINT when the LLM extraction supplies real
# coordinates -- there is no geocoder wired in, so this path is low-frequency in
# practice. 1.5 km keeps a POINT-scoped event from being treated as relevant to a site
# clear across a dense city; it is an unvalidated placeholder, same caveat Phase 1 gives
# its own fallback canonical_id collision risk (R1) -- tune against real coordinate data
# before trusting it load-bearing.
POINT_RADIUS_KM = 1.5

_EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def geography_gate(event: dict, site) -> tuple[bool, str | None]:
    """Returns (silence, reason). `site` is a phase2.models.Site row (or any object with
    .lat/.lng/.political_locality/.political_region)."""
    geo = event.get("geography") or {}
    gtype = geo.get("type")

    if gtype == "UNRESOLVED" or not gtype:
        return True, "event location is unknown (UNRESOLVED); cannot confirm geographic relevance"

    if gtype == "JURISDICTION":
        name = (geo.get("name") or event.get("jurisdiction") or "").strip().casefold()
        site_names = {
            (getattr(site, "political_locality", None) or "").casefold(),
            (getattr(site, "political_region", None) or "").casefold(),
        }
        if name and name in site_names:
            return False, None
        return True, (
            f"site's jurisdiction ({getattr(site, 'political_locality', None) or 'unknown'}) "
            f"does not match the event's jurisdiction ({geo.get('name') or event.get('jurisdiction')!r})"
        )

    if gtype == "POINT":
        lat, lng = geo.get("latitude"), geo.get("longitude")
        if lat is None or lng is None:
            return True, "event marked POINT but has no coordinates; treated as unresolved"
        distance_km = haversine_km(lat, lng, site.lat, site.lng)
        if distance_km <= POINT_RADIUS_KM:
            return False, None
        return True, f"site is {distance_km:.2f} km from the event's location, beyond the {POINT_RADIUS_KM} km radius"

    # POLYGON: Phase 1 never emits this in practice today (no geocoder wired in).
    # Point-in-polygon is not implemented for v1 -- treat conservatively rather than guess.
    return True, f"geography type {gtype!r} is not evaluated in this version; treated as unresolved"
