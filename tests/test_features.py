import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.structured_llm import StructuredQuality
from agent.graph import build_graph
from agent.session_store import SqliteSessionStore
from agent.service import ChatRequest, ChatService
from agent.validation import validate
from agent.reporting import facts_from, assemble
from agent.targets import resolve
from fixture_mcp import FixtureMCP
from fastapi import HTTPException


class TestMCP(FixtureMCP):
    def __init__(self, failure=None, no_match=False):
        super().__init__({"failure": failure or {}, "overrides": {}})
        self.no_match = no_match

    async def find_tool(self, name):
        return "ops"

    async def call_tool(self, name, arguments, server="ops"):
        if name != "search_runbooks":
            return await super().call_tool(name, arguments, server)
        if self.case["failure"].get("runbook"):
            raise ValueError("injected")
        if self.no_match:
            return {"status": "no_match", "evidence": []}
        system = arguments["system_name"]
        return {"status": "success", "evidence": [{
            "evidence_id": f"{system}:cpu-spike:0", "document_id": "cpu-spike",
            "chunk_id": "cpu-spike:0", "title": "개발용 문서", "incident_type": "cpu-spike",
            "text": "관측 결과를 확인한다.", "score": .8, "source_version": "test", "system_name": system}]}


class BrokenQuality:
    name = "broken"
    calls = 0
    async def report(self, *args):
        self.calls += 1
        return {"hypotheses": [], "actions": [{
            "system_name": "payments-service", "text": "잘못된 초안 비밀문장",
            "evidence_ids": ["nonexistent"], "prerequisites": "없음"}]}, {"usd": 0}


def graph(mcp=None, quality=None):
    return build_graph(SupervisorDeps(FastLLM("stub", 0),
        quality or StructuredQuality(QualityLLM("stub", 0)), mcp or TestMCP(), None, "improved"))


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_clarification_before_any_tool(self):
        mcp = TestMCP()
        r = await graph(mcp).ainvoke({"query": "로그인이 느려요", "events": []})
        self.assertEqual(r["status"], "needs_clarification")
        self.assertEqual(mcp.calls, [])
        self.assertEqual(resolve("payments-service와 billing-service").unresolved_names, ["billing-service"])
        self.assertEqual(resolve("notpayments-service").resolved_systems, [])

    async def test_partial_retains_success(self):
        r = await graph(TestMCP({"cache-service": "permanent"})).ainvoke(
            {"query": "payments-service와 cache-service 장애 점검", "events": []})
        self.assertEqual(r["status"], "partial")
        self.assertEqual({f["system_name"] for f in r["report"]["facts"]}, {"payments-service"})
        self.assertIn("cache-service: 모니터링 실패", r["final"])

    async def test_all_failed_no_fabricated_facts(self):
        r = await graph(TestMCP({"*": "timeout"})).ainvoke(
            {"query": "payments-service 장애", "events": []})
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["report"]["facts"], [])
        self.assertEqual(r["report"]["actions"], [])
        self.assertEqual(r["retry_count"], 2)

    async def test_search_failed_and_no_match(self):
        for mcp in [TestMCP({"runbook": "permanent"}), TestMCP(no_match=True)]:
            r = await graph(mcp).ainvoke({"query": "payments-service 장애", "events": []})
            self.assertEqual(r["status"], "partial")
            self.assertTrue(r["report"]["facts"])
            self.assertFalse(r["report"]["actions"])

    async def test_rewrite_limit_and_no_draft_in_stream(self):
        quality = BrokenQuality()
        output = []
        async for mode, delta in graph(quality=quality).astream(
                {"query": "payments-service 장애", "events": []}, stream_mode=["updates", "custom"]):
            if mode == "custom":
                output.append(delta)
            else:
                for change in delta.values():
                    if change.get("final"):
                        result = change
        self.assertEqual(quality.calls, 3)
        self.assertEqual(result["status"], "validation_failed")
        self.assertNotIn("잘못된 초안 비밀문장", result["final"])
        self.assertEqual(output, [])

    async def test_service_resume_replay_and_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"session.db"
            store = SqliteSessionStore(path)
            service = ChatService(graph(), store)
            req = ChatRequest(message="로그인 장애", thread_id="a", request_id="1")
            entry = service.start(req)
            with self.assertRaises(HTTPException):
                service.start(req.model_copy(update={"request_id": "2"}))
            await entry["task"]
            self.assertIs(service.start(req), entry)
            cid = entry["result"]["clarification"]["clarification_id"]
            store.close()
            store = SqliteSessionStore(path)
            self.assertEqual(store.pending("a")["clarification_id"], cid)
            service = ChatService(graph(), store)
            with self.assertRaises(HTTPException):
                service.start(ChatRequest(message="선택", thread_id="other", request_id="3",
                    clarification_id=cid, selected_systems=["auth-service"]))
            selection = ChatRequest(message="선택", thread_id="a", request_id="3",
                    clarification_id=cid, selected_systems=["auth-service"])
            resumed = service.start(selection)
            await resumed["task"]
            self.assertEqual(resumed["result"]["metadata"]["original_query"], "로그인 장애")
            self.assertEqual(resumed["result"]["report"]["systems"], ["auth-service"])
            self.assertIs(service.start(selection), resumed)
            with self.assertRaises(HTTPException):
                service.start(selection.model_copy(update={"request_id": "4"}))
            self.assertIsNone(store.pending("a"))
            store.close()

    async def test_expired_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SqliteSessionStore(Path(temp)/"db")
            store.save_pending("a", {"clarification_id": "expired", "original_query": "q"}, ttl=-1)
            with self.assertRaises(ValueError):
                store.consume_pending("a", "expired")
            store.close()

    async def test_transient_error_recovers(self):
        mcp = TestMCP({"payments-service": "transient_once"})
        r = await graph(mcp).ainvoke({"query": "payments-service 장애", "events": []})
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["retry_count"], 1)
        self.assertEqual(mcp.counts["payments-service"], 2)

    async def test_cancel_is_not_swallowed(self):
        from agent.nodes.investigation import run_task
        entered = asyncio.Event()
        async def wait_forever():
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(run_task("x", "monitoring", wait_forever, asyncio.Semaphore(1)))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_pending_replacement_invalidates_old_id(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SqliteSessionStore(Path(temp)/"db")
            store.save_pending("a", {"clarification_id": "old", "original_query": "old query"})
            store.save_pending("a", {"clarification_id": "new", "original_query": "new query"})
            with self.assertRaises(ValueError):
                store.consume_pending("a", "old")
            self.assertEqual(store.pending("a")["original_query"], "new query")
            store.close()


class ValidatorTests(unittest.TestCase):
    def test_tampering_is_rejected(self):
        facts = facts_from([{"observation_id": "obs:x", "system_name": "x", "status": "down",
                             "cpu_percent": 95, "memory_percent": 50, "alerts": []}])
        report = assemble(["x"], facts, {"hypotheses": [], "actions": []}, [], ["실패한 조회 있음"])
        self.assertEqual(validate(report, facts, [], ["x"], ["실패한 조회 있음"], 1)["status"], "passed")
        for mutation, code in [
            (lambda r: r["facts"][1].update(value=12), "FACT_MISMATCH"),
            (lambda r: r["facts"][0].update(system_name="y"), "UNKNOWN_SYSTEM"),
            (lambda r: r.update(limitations=[]), "MISSING_LIMITATION"),
        ]:
            bad = copy.deepcopy(report)
            mutation(bad)
            self.assertIn(code, [e["code"] for e in validate(bad, facts, [], ["x"], ["실패한 조회 있음"], 1)["errors"]])


if __name__ == "__main__":
    unittest.main()
