"""Scout — decides what to look at next.

This agent exists because two real bugs were both adaptive-investigation failures, not
field-list failures:

* `intersects_protected_area: True` scored a municipal golf course as disqualifying,
  because nothing went back for `protected_area_gap_status`. GAP 4 is nominal.
* `nearest_transmission_line_voltage_kv: absent` scored a site as having no grid,
  because nothing checked a second source. There was 230 kV at 1.3 km.

In both cases the *first* answer told you what the *second* question should be. That is
what the Scout does.

**It is mostly deterministic, and that is the design.** Mireye's own
`interpretation_hints` say things like "check `nearest_transmission_line_voltage_class`"
and "read `protected_area_gap_status`" — those follow-ups are documented, not inferred,
so a rules table encodes them faithfully and for free. The model tier is reserved for
the residual case where the rules have nothing to say and something still looks wrong.
"""

from __future__ import annotations

from typing import Any, Optional

from .. import scoring
from ..vicinity import policy_for
from .base import Investigation

#: Deterministic follow-up rules, each traceable to a documented Mireye hint.
#: (trigger_field, predicate, follow_up_fields, why)
FOLLOW_UPS: tuple = (
    (
        "intersects_protected_area",
        lambda v: bool(v),
        ["protected_area_gap_status", "protected_area_designation", "protected_area_name"],
        "PAD-US describes ownership intent, not buildability; GAP status is what makes "
        "the flag interpretable, and GAP 4 is nominal",
    ),
    (
        "nearest_transmission_line_voltage_kv",
        lambda v: v is None,
        ["nearest_transmission_line_voltage_class", "nearest_transmission_line_voltage_basis",
         "nearest_osm_substation_distance_m", "nearest_osm_substation_max_voltage_kv"],
        "null voltage is not 'no voltage' -- check the class, the basis and a second "
        "source before concluding there is no grid",
    ),
    (
        "in_karst_area",
        lambda v: bool(v),
        ["karst_exposure_class"],
        "exposure class is the load-bearing qualifier on the karst flag; buried karst is "
        "a far smaller foundation problem than exposed",
    ),
    (
        "btm_gas_candidacy_flag",
        lambda v: bool(v),
        ["nearest_class_i_area_distance_m", "nearest_class_i_area_name",
         "in_air_quality_nonattainment", "in_air_quality_maintenance"],
        "gas candidacy says nothing about whether a gas plant could be PERMITTED -- "
        "Class I proximity triggers PSD/FLM consultation within ~300 km",
    ),
    (
        "intersects_critical_habitat",
        lambda v: bool(v),
        ["critical_habitat_status", "critical_habitat_species", "critical_habitat_listing_status"],
        "the boolean alone does not say what is listed or at what status",
    ),
    (
        "nearest_power_plant_primary_fuel",
        lambda v: str(v).lower() in ("coal", "petroleum", "natural gas", "gas"),
        ["nearest_power_plant_capacity_mw", "nearest_power_plant_distance_m",
         "near_epa_repowering_site", "nearest_repowering_site_distance_m"],
        "a retiring thermal plant nearby implies inheritable interconnection, water "
        "rights and industrial zoning -- worth confirming",
    ),
    (
        "within_floodplain_polygon",
        lambda v: bool(v),
        ["fema_flood_zone", "flood_zone_subtype", "fema_base_flood_elevation"],
        "the zone and subtype determine whether this is a genuine constraint",
    ),
)


class Scout:
    """Plans the next fetch. Never fetches, never scores."""

    name = "scout"
    model_tier = "triage"   # only consulted when the rules table is silent

    def initial_fields(self, goal: str) -> list:
        """The opening question set for a goal."""
        return scoring.required_fields(goal)

    def follow_ups(self, observed: dict, already: list) -> list:
        """Deterministic second questions implied by the first answers.

        Returns `[(field, why)]`. Rules fire on what came back, so a site with no
        protected-area flag never pays for GAP status.
        """
        out: list = []
        seen = set(already)
        for trigger, predicate, fields, why in FOLLOW_UPS:
            if trigger not in observed:
                continue
            record = observed[trigger]
            status = record.get("status") if isinstance(record, dict) else None
            value = record.get("value") if isinstance(record, dict) else record
            # `failed` is not an answer, so it cannot trigger a follow-up.
            if status == "failed":
                continue
            try:
                fires = predicate(value)
            except Exception:  # noqa: BLE001 -- a malformed value must not kill the run
                fires = False
            if not fires:
                continue
            for f in fields:
                if f not in seen:
                    out.append((f, why))
                    seen.add(f)
        return out

    def coverage_gaps(self, vicinity: dict) -> list:
        """Fields whose ring shows a search-radius artefact rather than a fact.

        Purely deterministic -- `vicinity.aggregate` already computes the coverage note.
        Sending this to a model would add cost and variance for no information.
        """
        gaps = []
        for name, agg in (vicinity or {}).items():
            note = agg.get("coverage_note") or ""
            if "artefact" in note and policy_for(name).cls == "connectable":
                gaps.append((name, note))
        return gaps

    async def run(self, inv: Investigation, *, observed: Optional[dict] = None,
                  **kwargs: Any) -> Investigation:
        observed = observed or {}
        planned = self.follow_ups(observed, inv.fields_fetched)

        for name, note in self.coverage_gaps(inv.vicinity):
            inv.note(f"{name}: {note}")
            inv.ask(f"confirm {name} against an independent source")

        if planned:
            inv.record(
                self.name, "plan_follow_up",
                "; ".join(sorted({why for _, why in planned}))[:400],
                result={"fields": [f for f, _ in planned]},
            )
            for f, _ in planned:
                inv.ask(f"fetch {f}")
        else:
            inv.record(self.name, "no_follow_up",
                       "first answers imply no documented follow-up questions")
        return inv

    def pending_fields(self, inv: Investigation) -> list:
        """Fields the Scout has queued but that have not been fetched yet."""
        return [q[len("fetch "):] for q in inv.open_questions
                if q.startswith("fetch ") and q[len("fetch "):] not in inv.fields_fetched]
