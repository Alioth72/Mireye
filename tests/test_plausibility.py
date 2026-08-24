"""Protected-area record plausibility.

Mireye's PAD-US join returns `Papahanaumokuakea Marine National Monument` — a Pacific
marine reserve — for every coordinate between ~25.3°N and ~31.6°N regardless of longitude.
It wrongly flags Houston, New Orleans, Jacksonville, Tampa, Mobile, Baton Rouge, Corpus
Christi, Miami and San Antonio as GAP-2 conservation land, 4,400–5,550 miles from the
actual monument. Full write-up: `Plan/mireye-bug-report-padus-latitude-band.md`.

The failure is silent — `status: ok`, normal confidence — so nothing downstream has any
signal to distrust it. These tests pin the two guards that give us one.

The named quarantine is a workaround and should die when the vendor fixes it. The
self-contradiction check is the durable half: it needs to know nothing about Hawaii, so it
keeps working for defects nobody has found yet.
"""

from __future__ import annotations

import pytest

from phase2.scoring import protected_record_trustworthy, score
from tests.test_scoring import dp, site

PAPA = "Papahanaumokuakea Marine National Monument"


def reader(**fields):
    from phase2.scoring import _Reader
    return _Reader({k: dp(k, v) for k, v in fields.items()})


# ==========================================================================
# named quarantine — the known upstream defect
# ==========================================================================
def test_pacific_monument_in_texas_is_quarantined() -> None:
    trusted, why = protected_record_trustworthy(
        reader(protected_area_name=PAPA, political_region="Texas",
               protected_area_gap_status=2)
    )
    assert trusted is False
    assert "known upstream PAD-US defect" in why
    assert "Texas" in why


@pytest.mark.parametrize("region", ["Texas", "Florida", "Louisiana", "Alabama"])
def test_quarantine_covers_every_affected_state(region: str) -> None:
    trusted, _ = protected_record_trustworthy(
        reader(protected_area_name=PAPA, political_region=region,
               protected_area_gap_status=2)
    )
    assert trusted is False


def test_the_monument_is_still_trusted_in_hawaii() -> None:
    """The record is not fictional — it is a real monument in the wrong place. A
    quarantine that rejected it everywhere would be its own bug."""
    trusted, _ = protected_record_trustworthy(
        reader(protected_area_name=PAPA, political_region="Hawaii",
               protected_area_gap_status=2)
    )
    assert trusted is True


def test_unrelated_protected_areas_are_untouched() -> None:
    for name, region in [("Yellowstone National Park", "Wyoming"),
                         ("Gunnison National Forest", "Colorado"),
                         ("High Peaks Wilderness", "New York")]:
        trusted, _ = protected_record_trustworthy(
            reader(protected_area_name=name, political_region=region,
                   protected_area_gap_status=1)
        )
        assert trusted is True, name


# ==========================================================================
# self-contradiction — the durable guard
# ==========================================================================
def test_strict_conservation_on_developed_suburbia_is_distrusted() -> None:
    """A GAP-2 conservation unit does not contain developed land at ~900 homes/km².
    This check knows nothing about Hawaii and would catch an unrelated bad record."""
    trusted, why = protected_record_trustworthy(
        reader(protected_area_name="Some Refuge", political_region="Ohio",
               protected_area_gap_status=2, land_use_class="Developed",
               housing_units_density_per_km2=898.0)
    )
    assert trusted is False
    assert "self-contradictory" in why


def test_state_managed_wilderness_is_NOT_distrusted() -> None:
    """REGRESSION. An earlier version distrusted any GAP 1/2 record whose
    surface_management_agency read private_or_unknown. That field tracks FEDERAL surface
    management, so state parks, state forest preserves and NGO preserves legitimately
    have none while being genuinely protected.

    Adirondack High Peaks Wilderness is the case that caught it: GAP 1, New York State
    Forest Preserve, constitutionally "forever wild", 0.5 homes/km2 of Forest. The gate
    wrongly cleared it and its score rose from 0.005 to 0.185 -- ignoring real protection,
    which is the same class of error as the golf course, arriving from the other side."""
    trusted, _ = protected_record_trustworthy(
        reader(protected_area_name="High Peaks Wilderness", political_region="New York",
               protected_area_gap_status=1, surface_management_agency="private_or_unknown",
               land_use_class="Forest", housing_units_density_per_km2=0.48)
    )
    assert trusted is True


def test_federal_agency_signal_only_corroborates() -> None:
    """It may sharpen a developed-land finding; it may never trigger one alone."""
    trusted, why = protected_record_trustworthy(
        reader(protected_area_name="Some Refuge", political_region="Ohio",
               protected_area_gap_status=2, land_use_class="Developed",
               housing_units_density_per_km2=898.0,
               surface_management_agency="private_or_unknown")
    )
    assert trusted is False
    assert "Developed land" in why
    assert "surface management also reads" in why


def test_gap4_parkland_in_a_town_is_NOT_distrusted() -> None:
    """GAP 4 is city parks and military land — developed surroundings are exactly what
    you expect. Only GAP 1/2 claims are self-contradictory there, and over-triggering
    would re-break the Interbay golf-course case from the other direction."""
    trusted, _ = protected_record_trustworthy(
        reader(protected_area_name="Interbay Golf", political_region="Washington",
               protected_area_gap_status=4, land_use_class="Developed",
               housing_units_density_per_km2=900.0)
    )
    assert trusted is True


def test_wilderness_on_undeveloped_land_is_trusted() -> None:
    trusted, _ = protected_record_trustworthy(
        reader(protected_area_name="High Peaks Wilderness", political_region="New York",
               protected_area_gap_status=1, land_use_class="Forest",
               housing_units_density_per_km2=0.4, surface_management_agency="NPS")
    )
    assert trusted is True


def test_no_protected_record_is_trivially_trusted() -> None:
    assert protected_record_trustworthy(reader(political_region="Kansas"))[0] is True


# ==========================================================================
# effect on the score
# ==========================================================================
def test_quarantined_record_does_not_apply_its_penalty() -> None:
    """San Antonio scored 0.108 in the national spread test purely because of this
    record. The scoring was correct; the input was not."""
    affected = site(intersects_protected_area=True, protected_area_gap_status=2,
                    protected_area_name=PAPA, political_region="Texas",
                    land_use_class="Developed", housing_units_density_per_km2=898.0,
                    surface_management_agency="private_or_unknown")
    r = score("data_center_optionality", affected)
    assert r["components"]["clear"]["score"] >= 0.8


def test_the_reason_travels_into_the_output() -> None:
    """Silently dropping the penalty would be its own kind of dishonesty — the basis
    must say the record was ignored and why, so it reaches the alert."""
    affected = site(intersects_protected_area=True, protected_area_gap_status=2,
                    protected_area_name=PAPA, political_region="Florida")
    basis = score("data_center_optionality", affected)["components"]["clear"]["basis"]
    assert "IGNORED" in basis
    assert "Papahanaumokuakea" in basis


def test_a_genuine_protection_still_collapses_the_score() -> None:
    """The guard must not become a blanket excuse to ignore protected land."""
    real = site(intersects_protected_area=True, protected_area_gap_status=1,
                protected_area_name="Yellowstone National Park",
                political_region="Wyoming", land_use_class="Forest",
                housing_units_density_per_km2=0.1)
    assert score("data_center_optionality", real)["components"]["clear"]["score"] < 0.1
