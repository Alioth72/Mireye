"""Bundle definitions, and the parcel-trap lint.

With no budget gate in front of the API (team decision: credits are not scarce), this
test file is the only thing standing between a careless bundle edit and a 300-credit
charge per call. Treat a failure here as a release blocker.
"""

from __future__ import annotations

import pytest

from phase2.bundles import (
    BUNDLES,
    MAX_FIELDS_PER_FETCH,
    PARCEL_RECORD_CREDITS,
    PARCEL_RECORD_GROUP,
    UnknownBundle,
    bundle_fields,
    estimate_credits,
    fields_for,
    touches_parcel_record,
)


# --------------------------------------------------------------------------
# the parcel trap
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(BUNDLES))
def test_no_bundle_touches_the_parcel_record(name: str) -> None:
    """300 credits per location, charged once for ANY member of the group.

    The brief puts parcel/ownership fields out of scope entirely, so no bundle may
    reference one -- not even indirectly.
    """
    offenders = touches_parcel_record(BUNDLES[name])
    assert not offenders, (
        f"bundle {name!r} contains parcel_record group member(s) {sorted(offenders)}; "
        f"that costs {PARCEL_RECORD_CREDITS} credits per location"
    )


def test_parcel_group_has_all_nineteen_members() -> None:
    assert len(PARCEL_RECORD_GROUP) == 19


@pytest.mark.parametrize(
    "field",
    [
        "wetland_acres_on_parcel",
        "wetland_fraction_of_parcel",
        "developable_acres_proxy",
        "onsite_solar_potential_mwac_low",
        "onsite_solar_potential_mwac_high",
    ],
)
def test_the_five_disguised_parcel_fields_are_caught(field: str) -> None:
    """These five do not look like parcel fields. You cannot tell from the name, which
    is exactly why membership is asserted here rather than eyeballed."""
    assert field in PARCEL_RECORD_GROUP
    assert touches_parcel_record([field]) == {field}


def test_lookalike_fields_are_not_in_the_group() -> None:
    """`wetland_acres` and `intersects_protected_area` sit in the parcels LAYER but not
    in the metered GROUP -- 1 credit each. Confusing them the other way would make us
    drop fields we actually need."""
    for field in ("wetland_acres", "intersects_protected_area", "intersects_wetland"):
        assert field not in PARCEL_RECORD_GROUP


def test_estimate_credits_flags_the_parcel_cliff() -> None:
    assert estimate_credits(["slope_degrees", "elevation"]) == 2
    assert estimate_credits(["slope_degrees", "parcel_owner"]) == 1 + PARCEL_RECORD_CREDITS


# --------------------------------------------------------------------------
# bundle shape
# --------------------------------------------------------------------------
EXPECTED_CREDITS = {
    "grid": 6,
    "telecom": 3,
    "terrain": 3,
    "water": 4,
    "constraints": 3,
    "access": 3,
    "boundaries": 4,
}


@pytest.mark.parametrize("name,expected", sorted(EXPECTED_CREDITS.items()))
def test_bundle_credit_costs_match_the_design_doc(name: str, expected: int) -> None:
    assert estimate_credits(BUNDLES[name]) == expected


def test_every_bundle_is_documented() -> None:
    assert set(BUNDLES) == set(EXPECTED_CREDITS)


@pytest.mark.parametrize("name", sorted(BUNDLES))
def test_bundle_fields_are_unique(name: str) -> None:
    fields = BUNDLES[name]
    assert len(fields) == len(set(fields))


def test_full_data_center_selection_fits_one_fetch() -> None:
    """The heaviest recommended selection (sec. 10.1) must stay under the 50-field cap
    so it is one call, not two."""
    selection = fields_for(["grid", "telecom", "terrain", "water", "constraints"])
    assert len(selection) <= MAX_FIELDS_PER_FETCH
    assert estimate_credits(selection) == 19


def test_every_bundle_alone_fits_one_fetch() -> None:
    for name, fields in BUNDLES.items():
        assert len(fields) <= MAX_FIELDS_PER_FETCH, name


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def test_fields_for_dedupes_and_preserves_order() -> None:
    once = fields_for(["terrain"])
    twice = fields_for(["terrain", "terrain"])
    assert once == twice
    assert twice[0] == "slope_degrees"


def test_bess_selection_excludes_fiber() -> None:
    """Fiber is decisive for a data-centre moratorium and noise for a BESS one. This is
    why Phase 1 must keep `subject` in its emitted event."""
    bess = fields_for(["grid", "terrain", "water", "constraints"])
    assert "fiber_broadband_available" not in bess
    assert estimate_credits(bess) == 16


def test_unknown_bundle_names_itself() -> None:
    with pytest.raises(UnknownBundle) as exc:
        bundle_fields("data_center_moratorium")
    # There is no event-shaped bundle, and the error should say so helpfully.
    assert "unknown bundle" in str(exc.value)
    assert "grid" in str(exc.value)
