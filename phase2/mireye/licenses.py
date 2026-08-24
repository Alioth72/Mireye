"""Source -> licence resolution.

Our derived optionality scores ARE derived values, and an alert is redistribution, so
the ODbL share-alike/attribution obligation reaches all the way through to Phase 3's
output. The licence is therefore captured at write time from the RUNTIME `source`.

Never infer licence from the field name: `nearest_transmission_line_*` is EIA/HIFLD
while `nearest_osm_transmission_line_*` is ODbL, and runtime provenance can be more
specific than the catalog default (the reference documents `elevation` falling back
from USGS_EPQS to USGS_3DEP_COG).
"""

from __future__ import annotations

ODBL = "ODbL-1.0 (attribution + share-alike)"
CDLA_PERMISSIVE = "CDLA-Permissive-2.0 (attribution)"
APACHE_2 = "Apache-2.0"
PUBLIC_DOMAIN_US_GOV = "US Government work (public domain)"
LICENSED_PROPRIETARY = "Licensed (redistribution restricted)"

#: Attribution strings that must survive into any redistributed derived value.
ATTRIBUTION = {
    ODBL: "© OpenStreetMap contributors",
}

# Matched as case-insensitive substrings against the runtime `source` string.
# Order matters: the first match wins, so put narrow patterns before broad ones.
_RULES: tuple[tuple[str, str], ...] = (
    ("openstreetmap", ODBL),
    ("openinframap", ODBL),
    ("osm", ODBL),
    ("overture_places", CDLA_PERMISSIVE),
    ("overture_transportation", ODBL),
    ("overture_buildings", ODBL),
    ("overture_divisions", ODBL),
    ("overture", ODBL),  # conservative default for unlabelled Overture themes
    ("foursquare", APACHE_2),
    ("regrid", LICENSED_PROPRIETARY),
    ("usgs", PUBLIC_DOMAIN_US_GOV),
    ("noaa", PUBLIC_DOMAIN_US_GOV),
    ("nrel", PUBLIC_DOMAIN_US_GOV),
    ("census", PUBLIC_DOMAIN_US_GOV),
    ("fema", PUBLIC_DOMAIN_US_GOV),
    ("epa", PUBLIC_DOMAIN_US_GOV),
    ("eia", PUBLIC_DOMAIN_US_GOV),
    ("hifld", PUBLIC_DOMAIN_US_GOV),
    ("faa", PUBLIC_DOMAIN_US_GOV),
    ("bls", PUBLIC_DOMAIN_US_GOV),
    ("bts", PUBLIC_DOMAIN_US_GOV),
    ("blm", PUBLIC_DOMAIN_US_GOV),
    ("calfire", PUBLIC_DOMAIN_US_GOV),
    ("nhd", PUBLIC_DOMAIN_US_GOV),
    ("egrid", PUBLIC_DOMAIN_US_GOV),
)


def license_for(source: str | None) -> str | None:
    """Resolve a licence string from a runtime `source`.

    Returns ``None`` for an unrecognised source rather than guessing. An unknown
    licence is a thing to look up, not a thing to assume is permissive.
    """
    if not source:
        return None
    needle = source.casefold()
    for pattern, licence in _RULES:
        if pattern in needle:
            return licence
    return None


def attribution_for(license_str: str | None) -> str | None:
    if not license_str:
        return None
    return ATTRIBUTION.get(license_str)


def share_alike(license_str: str | None) -> bool:
    """True when a derived value carries share-alike obligations downstream."""
    return license_str == ODBL
