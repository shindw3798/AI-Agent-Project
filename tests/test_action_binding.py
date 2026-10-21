import copy
import json
import unittest
from pathlib import Path
from test_features import graph
from agent.action_catalog import catalog, materialize, ActionSelectionError, REVIEW_GATE
from agent.schemas import NarrativeSelection
from agent.validation import validate
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]


def evidence(text, eid="auth-service:high-latency:0"):
    return {"evidence_id": eid, "system_name": "auth-service", "text": text}


class ActionBindingTests(unittest.IsolatedAsyncioTestCase):
    def test_t04_source_steps_remain_separate_and_ordered(self):
        text = (ROOT / "data/runbooks/high-latency.md").read_text(encoding="utf-8")
        e = evidence(text)
        steps = catalog([e])
        s4, s5 = e["evidence_id"] + ":step:4", e["evidence_id"] + ":step:5"
        result = materialize({"hypotheses": [], "actions": [
            {"source_step_id": s5}, {"source_step_id": s4}]}, [e])
        self.assertEqual([a["source_step_id"] for a in result["actions"]], [s4, s5])
        self.assertIn("내려오지 않으면", steps[s4]["text"])
        self.assertNotIn("정상화 후", steps[s4]["text"])
        self.assertTrue(steps[s5]["text"].startswith("정상화 후"))
        self.assertNotIn("내려오지 않으면", steps[s5]["text"])
        self.assertEqual(steps[s5]["prerequisites"], REVIEW_GATE)

    def test_cannot_submit_free_action_or_condition(self):
        for field in ("text", "prerequisites", "system_name", "evidence_ids"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                NarrativeSelection.model_validate({"hypotheses": [], "actions": [
                    {"source_step_id": "x:step:1", field: "untrusted"}]})

    def test_unknown_duplicate_and_ambiguous_ids_rejected(self):
        e = evidence("1. 확인한다.\n2. 정상화 후 기록한다.")
        sid = e["evidence_id"] + ":step:1"
        for ids in (["other:step:1"], [sid, sid]):
            with self.assertRaises(ActionSelectionError):
                materialize({"hypotheses": [], "actions": [{"source_step_id": s} for s in ids]}, [e])
        with self.assertRaises(ActionSelectionError):
            catalog([evidence("1. 확인한다.\n1. 다른 조치다.")])

    def test_every_original_runbook_step_preserves_whole_sentence(self):
        expected_counts = {"db-down": 3, "cpu-spike": 5, "disk-full": 5, "high-latency": 5,
                           "memory-leak": 5, "network-timeout": 5}
        for path in (ROOT / "data/runbooks").glob("*.md"):
            text = path.read_text(encoding="utf-8")
            steps = catalog([evidence(text)])
            self.assertEqual(len(steps), expected_counts[path.stem])
            for item in steps.values():
                self.assertIn(item["text"], text)
                self.assertEqual(item["prerequisites"], REVIEW_GATE)

    def test_multiline_and_unseen_document_preserve_context(self):
        text = "# 새 문서\n1. 점검을 시작한다.\n2. 승인된 경우에만 작업한다.\n   승인되지 않으면 중단한다.\n3. 완료 후 확인한다."
        steps = catalog([evidence(text, "new")])
        self.assertEqual(steps["new:step:2"]["text"], "승인된 경우에만 작업한다.\n승인되지 않으면 중단한다.")
        fallback = catalog([evidence("조건 확인 후 처리한다.\n예외가 있으면 중단한다.", "plain")])
        self.assertIn("예외가 있으면 중단한다.", fallback["plain:step:1"]["text"])

    async def test_condition_tampering_rejected_after_materialization(self):
        r = await graph().ainvoke({"query": "payments-service 장애", "events": []})
        for field, value in [("prerequisites", "다른 단계의 조건"), ("text", "조건 없이 즉시 실행"),
                             ("system_name", "auth-service"), ("source_step_id", "unknown")]:
            report = copy.deepcopy(r["report"])
            report["actions"][0][field] = value
            verdict = validate(report, r["facts"], r["evidence"], r["targets"], r["limitations"], 1)
            self.assertIn("ACTION_SOURCE_MISMATCH", [e["code"] for e in verdict["errors"]])

    def test_recorded_v2_t04_mixed_condition_is_rejected(self):
        path = ROOT / "tests/fixtures/v2/openai-small.T04.r1.case.json"
        r = json.loads(path.read_text(encoding="utf-8"))["result"]
        verdict = validate(r["report"], r["facts"], r["evidence"], r["targets"], r["limitations"], 1)
        self.assertIn("ACTION_SOURCE_MISMATCH", [e["code"] for e in verdict["errors"]])

    async def test_bad_selection_retry_receives_raw_previous_choice(self):
        class Writer:
            async def report(self, query, facts, evidence, feedback, previous):
                if previous is None:
                    return {"hypotheses": [], "actions": [{"source_step_id": "unknown"}]}, {"usd": 0}
                assert previous["actions"][0]["source_step_id"] == "unknown"
                assert feedback[0]["code"] == "INVALID_ACTION_STEP"
                return {"hypotheses": [], "actions": [
                    {"source_step_id": evidence[0]["evidence_id"] + ":step:1"}]}, {"usd": 0}
        r = await graph(quality=Writer()).ainvoke({"query": "payments-service 장애", "events": []})
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["validation_attempts"], 2)
