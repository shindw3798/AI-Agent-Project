import asyncio
import json
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from agent.service import ChatService
from agent.session_store import SqliteSessionStore
from retrieval.index import RunbookIndex, save
from test_features import graph, TestMCP
import server


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteSessionStore(Path(self.temp.name)/"db")
        service = ChatService(graph(TestMCP()), self.store)
        @asynccontextmanager
        async def lifespan(app):
            app.state.service = service
            app.state.models = {"backend": "test"}
            yield
        self.original = server.app.router.lifespan_context
        server.app.router.lifespan_context = lifespan
        self.client = TestClient(server.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        server.app.router.lifespan_context = self.original
        self.store.close()
        self.temp.cleanup()

    def test_post_sse_same_result_and_no_tokens(self):
        req = {"message": "payments-service 장애", "thread_id": "a", "request_id": "1"}
        posted = self.client.post("/chat", json=req)
        self.assertEqual(posted.status_code, 200)
        # Replay the same request through SSE: same payload, no execution and no draft tokens.
        streamed = self.client.get("/chat/stream", params=req)
        records = [json.loads(line[6:]) for line in streamed.text.splitlines()
                   if line.startswith("data: ") and line != "data: [DONE]"]
        final = records[-1]
        self.assertEqual(final.pop("type"), "FinalAnswer")
        self.assertEqual(final, posted.json())
        self.assertNotIn("AnswerToken", [r.get("type") for r in records])
        self.assertIn("data: [DONE]", streamed.text)
        self.assertEqual(self.client.post("/chat", json={**req, "message": "different"}).status_code, 409)

    def test_new_post_and_sse_contract_match(self):
        query = "payments-service 장애"
        posted = self.client.post("/chat", json={"message": query, "thread_id": "a", "request_id": "a"}).json()
        streamed = self.client.get("/chat/stream", params={"message": query, "thread_id": "b", "request_id": "b"})
        final = [json.loads(l[6:]) for l in streamed.text.splitlines()
                 if l.startswith("data: {") and '"FinalAnswer"' in l][0]
        self.assertEqual(final["report"], posted["report"])
        self.assertEqual(final["status"], posted["status"])
        self.assertEqual(final["validation"], posted["validation"])

    def test_invalid_selection_before_sse(self):
        response = self.client.get("/chat/stream", params={
            "message": "select", "thread_id": "a", "request_id": "1",
            "selected_systems": '["fake-system"]', "clarification_id": "bad"})
        self.assertEqual(response.status_code, 422)


class RetrievalTests(unittest.TestCase):
    def test_stale_index_is_error_not_no_match(self):
        with tempfile.TemporaryDirectory() as temp:
            index = RunbookIndex(index_path=Path(temp)/"index.json")
            original = json.loads((Path(__file__).resolve().parents[1]/"data/runbook_index.json").read_text())
            original["source_hashes"] = {}
            save(index.path, original)
            with self.assertRaisesRegex(ValueError, "INDEX_MISMATCH"):
                index.search("q", "x", {"status": "healthy", "alerts": []})

    def test_healthy_needs_no_embedding_request(self):
        class ForbiddenEmbedder:
            def embed(self, texts):
                raise AssertionError("No embedding should be called")
        result = RunbookIndex(embedder=ForbiddenEmbedder()).search(
            "정상 시스템", "web-01", {"status": "healthy", "alerts": []})
        self.assertEqual(result, {"status": "no_match", "evidence": [], "error_code": None})


if __name__ == "__main__":
    unittest.main()
