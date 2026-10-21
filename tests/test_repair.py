"""Regression tests use recorded v1 failures; these are not independent holdouts."""
import copy
import json
import unittest
from pathlib import Path
from test_features import graph, TestMCP
from agent.structured_llm import StructuredQuality
from agent.validation import validate
from agent.reporting import assemble
from agent.schemas import Narrative
from pydantic import ValidationError

RESULTS = Path(__file__).resolve().parent / "fixtures/v1"


class RepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_previous_draft_and_errors_reach_transport(self):
        class Transport:
            name = "openai:stub"
            def _chat(self, system, user, **kwargs):
                self.received = json.loads(user)
                return '{"hypotheses": [], "actions": []}', {"usd": 0}
        transport = Transport()
        previous = {"hypotheses": [{"text": "unsupported"}], "actions": []}
        errors = [{"field_path": "hypotheses.0.evidence_ids", "code": "INVALID_SCHEMA"}]
        await StructuredQuality(transport).report("query", [], [], errors, previous)
        self.assertEqual(transport.received["previous_draft"], previous)
        self.assertEqual(transport.received["validation_feedback"], errors)

    async def test_graph_repair_keeps_valid_action_and_records_both_verdicts(self):
        class Repairing:
            async def report(self, query, facts, evidence, feedback, previous):
                action = {"source_step_id": evidence[0]["evidence_id"] + ":step:1"}
                if previous is None:
                    return {"hypotheses": [{"text": "unsupported", "fact_ids": [facts[0]["fact_id"]],
                                           "evidence_ids": []}], "actions": [action]}, {"usd": 0}
                assert previous["actions"] == [action]
                assert feedback[0]["field_path"] == "hypotheses.0.evidence_ids"
                return {"hypotheses": [], "actions": previous["actions"]}, {"usd": 0}
        result = await graph(quality=Repairing()).ainvoke({"query": "payments-service 장애", "events": []})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["validation_attempts"], 2)
        self.assertEqual([v["status"] for v in result["validation_history"]], ["failed", "passed"])
        self.assertEqual(len(result["report"]["actions"]), 1)
        self.assertNotIn("unsupported", result["final"])

    async def test_five_recorded_failures_still_rejected(self):
        for case_id in ["T01", "T04", "T05", "T11", "T26"]:
            with self.subTest(case=case_id):
                row = json.loads((RESULTS / f"openai-small.{case_id}.r1.case.json").read_text(encoding="utf-8"))
                drafts = [c["output"] for c in row["model_calls"] if c["stage"] == "report"]
                self.assertEqual(len(drafts), 3)
                for draft in drafts:
                    with self.assertRaises(ValidationError):
                        Narrative.model_validate(draft)

    async def test_empty_report_cannot_game_validation(self):
        result = await graph().ainvoke({"query": "payments-service 장애", "events": []})
        report = assemble(result["targets"], result["facts"], {"hypotheses": [], "actions": []},
                          result["evidence"], result["limitations"])
        verdict = validate(report, result["facts"], result["evidence"], result["targets"],
                           result["limitations"], 1)
        self.assertIn("MISSING_GROUNDED_ACTION", [e["code"] for e in verdict["errors"]])

    async def test_other_system_evidence_cannot_support_hypothesis(self):
        result = await graph().ainvoke({"query": "payments-service와 cache-service 장애", "events": []})
        report = copy.deepcopy(result["report"])
        fact = next(f for f in result["facts"] if f["system_name"] == "payments-service")
        evidence = next(e for e in result["evidence"] if e["system_name"] == "cache-service")
        report["hypotheses"] = [{"text": "두 시스템의 공통 원인 후보",
                                "fact_ids": [fact["fact_id"]], "evidence_ids": [evidence["evidence_id"]]}]
        verdict = validate(report, result["facts"], result["evidence"], result["targets"],
                           result["limitations"], 1)
        self.assertIn("CROSS_SYSTEM_HYPOTHESIS", [e["code"] for e in verdict["errors"]])
