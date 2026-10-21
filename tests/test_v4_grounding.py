import copy
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retrieval.index import RunbookIndex
from retrieval.applicability import applicable
from agent.reporting import facts_from, assemble, render
from agent.action_catalog import catalog
from agent.validation import validate


class GroundingTests(unittest.TestCase):
    def test_semantic_similarity_cannot_override_missing_prerequisite(self):
        class Forbidden:
            def embed(self, texts):
                raise AssertionError("No eligible runbook: no embedding necessary")
        for alert in ["backup restore checksum mismatch", "KMS key suspended",
                      "license entitlement expired", "hardware fan malfunction"]:
            with self.subTest(alert=alert):
                r = RunbookIndex(embedder=Forbidden()).search(
                    "database down CPU high", "x", {"status": "degraded", "alerts": [alert]})
                self.assertEqual(r["status"], "no_match")
                self.assertEqual(r["evidence"], [])

    def test_all_six_supported_incidents_remain_eligible(self):
        samples = {"cpu-spike": "CPU 91% (threshold 80%)",
                   "memory-leak": "memory usage climbing steadily over 6h",
                   "disk-full": "disk space low", "high-latency": "p95 latency 2.4s",
                   "network-timeout": "upstream timeout to x (504)",
                   "db-down": "database down"}
        for doc, alert in samples.items():
            self.assertTrue(applicable(doc, [alert]), doc)
        self.assertFalse(applicable("db-down", ["backup restore checksum mismatch"]))

    def test_search_ranks_only_applicable_documents(self):
        class Constant:
            def embed(self, texts):
                return [[1.0] + [0.0]*511 for _ in texts]
        index = RunbookIndex(embedder=Constant())
        # Deliberately make an unrelated DB document score better than CPU.
        data = index.load()
        for i, doc in enumerate(data["documents"]):
            data["vectors"][i] = ([1.0] if doc["document_id"] == "db-down" else [.8]) + [0.0]*511
        index.load = lambda: data
        r = index.search("DB down", "x", {"status": "degraded", "alerts": ["CPU 95% high"]})
        self.assertEqual([e["document_id"] for e in r["evidence"]], ["cpu-spike"])
        r = index.search("DB down", "x", {"status": "down", "alerts": ["database down"]})
        self.assertEqual([e["document_id"] for e in r["evidence"]], ["db-down"])

    def test_empty_alert_is_explicit_and_failure_does_not_become_healthy(self):
        obs = {"observation_id": "obs:x", "system_name": "x", "status": "healthy",
               "cpu_percent": 12., "memory_percent": 30., "alerts": []}
        facts = facts_from([obs])
        self.assertEqual(facts[-1]["metric_name"], "alerts_empty")
        report = assemble(["x"], facts, {"hypotheses": [], "actions": []}, [], [])
        self.assertIn("알림: 없음", render(report))
        failed = assemble(["x"], [], {"hypotheses": [], "actions": []}, [], [])
        self.assertNotIn("알림: 없음", render(failed))
        bad = copy.deepcopy(report)
        bad["facts"].pop()
        self.assertEqual(validate(bad, facts, [], ["x"], [], 1)["status"], "failed")

    def test_causal_prose_is_not_published_and_conditions_unchanged(self):
        e = {"evidence_id": "x:high-latency:0", "system_name": "x",
             "document_id": "high-latency", "chunk_id": "high-latency:0", "title": "응답 지연",
             "incident_type": "high-latency", "score": .8, "source_version": "test",
             "text": "1. 5분 후에도 느리면 분산한다.\n2. 정상화 후 기록한다."}
        actions = list(catalog([e]).values())
        report = assemble(["x", "y"], [], {
            "hypotheses": [{"text": "개발자 실수 때문에 장애가 발생했다.",
                            "fact_ids": ["obs:x:status"], "evidence_ids": [e["evidence_id"]]}],
            "actions": actions}, [e], [])
        self.assertNotIn("개발자 실수", str(report))
        self.assertIn("가능성이 있습니다", report["hypotheses"][0]["text"])
        self.assertEqual(report["actions"], actions)
        self.assertIn("공통 원인을 확정할 근거가 부족", render(report))


if __name__ == "__main__":
    unittest.main()
