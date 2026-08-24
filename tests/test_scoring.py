from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from monitor_decision.agentic import AgentConfig, decide
from monitor_decision.models import AlertDecision, DecisionRequest, ImpactDirection
from monitor_decision.scoring import score_decision


ROOT = Path(__file__).resolve().parents[1]


def load_example(name: str) -> DecisionRequest:
    return DecisionRequest.from_dict(json.loads((ROOT / "examples" / name).read_text(encoding="utf-8")))


class ScoringTests(unittest.TestCase):
    def test_high_optionality_proposed_moratorium_alerts(self) -> None:
        response = score_decision(load_example("data_center_moratorium_request.json"))

        self.assertEqual(response.decision, AlertDecision.ALERT)
        self.assertGreaterEqual(response.materiality_score, 62)
        self.assertEqual(response.impact_direction, ImpactDirection.NEGATIVE)
        self.assertEqual(response.event_stage.value, "proposed")
        self.assertFalse(response.missing_fields)

    def test_constrained_site_stays_quiet_even_after_adoption(self) -> None:
        response = score_decision(load_example("quiet_floodplain_request.json"))

        self.assertEqual(response.decision, AlertDecision.QUIET)
        self.assertLess(response.materiality_score, 42)
        self.assertTrue(response.quiet_reason)

    def test_missing_core_fields_pushes_to_review(self) -> None:
        payload = json.loads((ROOT / "examples" / "data_center_moratorium_request.json").read_text(encoding="utf-8"))
        del payload["mireye"]["fields"]["nearest_substation_distance_m"]
        del payload["mireye"]["fields"]["fiber_broadband_available"]
        del payload["mireye"]["fields"]["intersects_wetland"]

        response = score_decision(DecisionRequest.from_dict(payload))

        self.assertEqual(response.decision, AlertDecision.REVIEW)
        self.assertEqual(
            set(response.missing_fields),
            {
                "nearest_substation_distance_m",
                "fiber_broadband_available",
                "intersects_wetland",
            },
        )

    def test_adjacent_moratorium_is_positive_spillover(self) -> None:
        payload = json.loads((ROOT / "examples" / "data_center_moratorium_request.json").read_text(encoding="utf-8"))
        payload["event"]["scope"]["relation_to_site"] = "adjacent"

        response = score_decision(DecisionRequest.from_dict(payload))

        self.assertEqual(response.impact_direction, ImpactDirection.POSITIVE)
        self.assertEqual(response.decision, AlertDecision.ALERT)

    def test_agentic_mode_falls_back_to_rules_without_keys(self) -> None:
        payload = json.loads((ROOT / "examples" / "data_center_moratorium_request.json").read_text(encoding="utf-8"))
        previous_openai = os.environ.pop("OPENAI_API_KEY", None)
        previous_gemini = os.environ.pop("GEMINI_API_KEY", None)
        previous_google = os.environ.pop("GOOGLE_API_KEY", None)
        try:
            response = decide(DecisionRequest.from_dict(payload), AgentConfig(enabled=True))
        finally:
            if previous_openai is not None:
                os.environ["OPENAI_API_KEY"] = previous_openai
            if previous_gemini is not None:
                os.environ["GEMINI_API_KEY"] = previous_gemini
            if previous_google is not None:
                os.environ["GOOGLE_API_KEY"] = previous_google

        self.assertEqual(response["decision_source"], "rules_fallback")
        self.assertEqual(response["decision"], "alert")
        self.assertTrue(response["agent"]["enabled"])

    def test_phase1_phase2_contract_payload_maps_correctly(self) -> None:
        payload = {
            "event": {
                "event_id": "evt_123",
                "event_type": "MORATORIUM",
                "stage": "REJECTED",
                "title": "Rejected data center moratorium",
                "subject": "data centers",
                "jurisdiction": "Seattle",
                "confidence": 0.91,
                "evidence": [
                    {
                        "source_url": "https://seattle.legistar.example/item",
                        "passage": "The council rejected the proposed moratorium.",
                    }
                ],
                "scope": {"relation_to_site": "inside", "distance_m": 0},
            },
            "site": {"id": "site-1", "lat": 47.6, "lng": -122.3},
            "physical": {
                "datapoints": [
                    {"field_name": "nearest_transmission_line_voltage_kv", "value": 230, "status": "ok", "license": "public-domain"},
                    {"field_name": "nearest_transmission_line_distance_m", "value": 900, "status": "ok"},
                    {"field_name": "nearest_substation_distance_m", "value": 2000, "status": "ok"},
                    {"field_name": "fiber_broadband_available", "value": True, "status": "ok"},
                    {"field_name": "slope_degrees", "value": 3, "status": "ok"},
                    {"field_name": "within_floodplain_polygon", "status": "absent"},
                    {"field_name": "intersects_wetland", "status": "absent"},
                    {"field_name": "intersects_protected_area", "status": "absent"},
                ]
            },
        }

        response = score_decision(DecisionRequest.from_dict(payload))

        self.assertEqual(response.decision, AlertDecision.ALERT)
        self.assertEqual(response.impact_direction, ImpactDirection.POSITIVE)
        self.assertEqual(response.event_stage.value, "rejected")
        self.assertFalse(response.missing_fields)
        self.assertEqual(response.citations[1]["license"], "public-domain")

    def test_phase2_failed_datapoint_counts_as_missing(self) -> None:
        payload = json.loads((ROOT / "examples" / "data_center_moratorium_request.json").read_text(encoding="utf-8"))
        payload["mireye"] = {
            "datapoints": [
                {"field_name": name, **value}
                for name, value in payload.pop("mireye")["fields"].items()
            ]
        }
        for item in payload["mireye"]["datapoints"]:
            if item["field_name"] == "intersects_wetland":
                item.pop("value", None)
                item["status"] = "failed"

        response = score_decision(DecisionRequest.from_dict(payload))

        self.assertIn("intersects_wetland", response.missing_fields)
        self.assertEqual(response.decision, AlertDecision.REVIEW)


if __name__ == "__main__":
    unittest.main()
