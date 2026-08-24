"""
Geography resolution.

Per the spec: if a record mentions a geographic area but we can't reliably
resolve it to coordinates/polygon, mark it UNRESOLVED rather than
hallucinating geometry. No geocoding is wired in for MVP -- POINT/POLYGON
only get set when the LLM extraction (or, someday, a real geocoder) supplies
real coordinates.
"""

from __future__ import annotations

from .models import GeographyType

DEFAULT_JURISDICTION = "Seattle"


def resolve_geography(extracted: dict | None) -> tuple[GeographyType, dict]:
    """
    extracted: the `geographic_scope` dict from ExtractedEvent, e.g.
        {"type": "JURISDICTION", "name": "Seattle"}
        {"type": "POINT", "latitude": 47.6, "longitude": -122.3}
    or None if extraction didn't produce one / wasn't run.

    Returns (GeographyType, geography_json) ready to store on Event.
    """
    if not extracted:
        return GeographyType.JURISDICTION, {"name": DEFAULT_JURISDICTION}

    raw_type = (extracted.get("type") or "").upper()

    if raw_type == "POINT" and extracted.get("latitude") is not None and extracted.get("longitude") is not None:
        return GeographyType.POINT, {
            "latitude": extracted["latitude"],
            "longitude": extracted["longitude"],
        }

    if raw_type == "POLYGON" and extracted.get("geojson"):
        return GeographyType.POLYGON, {"geojson": extracted["geojson"]}

    if raw_type == "JURISDICTION":
        return GeographyType.JURISDICTION, {"name": extracted.get("name") or DEFAULT_JURISDICTION}

    # Explicitly refused to guess -- e.g. LLM said "some neighborhood" but
    # gave no coordinates. Don't invent geometry.
    return GeographyType.UNRESOLVED, {}
