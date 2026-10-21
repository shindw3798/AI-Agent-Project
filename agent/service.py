"""POST와 SSE 요청 실행, 중복 요청 재사용과 스레드별 실행 제어."""
import asyncio
import hashlib
import json
from collections import OrderedDict
from fastapi import HTTPException
from pydantic import Field, model_validator
from .schemas import Contract, RequestResult
from .targets import KNOWN_SYSTEMS


class ChatRequest(Contract):
    message: str = Field(min_length=1, max_length=12000)
    thread_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    clarification_id: str | None = None
    selected_systems: list[str] | None = None

    @model_validator(mode="after")
    def selection(self):
        if bool(self.clarification_id) != bool(self.selected_systems):
            raise ValueError("선택 ID와 시스템 목록을 함께 제공하세요.")
        if self.selected_systems and not set(self.selected_systems) <= set(KNOWN_SYSTEMS):
            raise ValueError("미등록 시스템입니다.")
        return self


class ChatService:
    def __init__(self, graph, store):
        self.graph, self.store = graph, store
        self.active = set()
        self.requests = OrderedDict()

    def start(self, req):
        # 재연결은 같은 결과를 돌려준다. 같은 ID로 다른 질문을 보내는 경우만 거부한다.
        key = (req.thread_id, req.request_id)
        fingerprint = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
        if key in self.requests:
            entry = self.requests[key]
            if entry["fingerprint"] != fingerprint:
                raise HTTPException(409, "같은 request_id에 다른 입력을 사용할 수 없습니다.")
            return entry
        if req.thread_id in self.active:
            raise HTTPException(409, "이 대화의 요청이 실행 중입니다.")
        query, selected, pending = req.message, req.selected_systems, None
        if req.clarification_id:
            try:
                pending = self.store.consume_pending(req.thread_id, req.clarification_id)
                query = pending["original_query"]
            except ValueError:
                raise HTTPException(409, "만료되었거나 이미 처리된 대상 선택입니다.")
        else:
            self.store.cancel_pending(req.thread_id)
        self.active.add(req.thread_id)
        entry = {"fingerprint": fingerprint, "events": [], "result": None, "changed": asyncio.Event(),
                 "selection_context": pending}
        self.requests[key] = entry
        entry["task"] = asyncio.create_task(self.run(req, query, selected, entry))
        # 처리 중인 요청은 캐시에서 제거하지 않는다.
        for old in list(self.requests):
            if len(self.requests) <= 500:
                break
            if self.requests[old]["result"] is not None:
                del self.requests[old]
        return entry

    async def run(self, req, query, selected, entry):
        result, seen = {}, 0
        try:
            async for delta in self.graph.astream({
                    "query": query, "thread_id": req.thread_id, "request_id": req.request_id,
                    "selected_systems": selected, "history": self.store.load(req.thread_id),
                    "events": [], "cost_usd": 0}, stream_mode="updates"):
                for change in delta.values():
                    result.update(change)
                    if "events" in change:
                        for event in change["events"][seen:]:
                            entry["events"].append({**event, "request_id": req.request_id})
                        seen = len(change["events"])
                entry["changed"].set()
            if result.get("clarification"):
                self.store.save_pending(req.thread_id, result["clarification"])
            payload = RequestResult(
                request_id=req.request_id, thread_id=req.thread_id, status=result["status"],
                reply=result["final"], report=result.get("report"), clarification=result.get("clarification"),
                validation=result.get("validation", {"status": "not_run", "attempt": 0, "errors": []}),
                task_results=result.get("task_results", []),
                metadata={"route": result.get("route"), "retries": result.get("retry_count", 0),
                          "implementation_version": "v5",
                          "validation_history": result.get("validation_history", []),
                          "validation_attempts": result.get("validation_attempts", 0),
                          "original_query": query, "selected_systems": selected,
                          "synthetic_cost_usd": result.get("cost_usd", 0)})
            self.store.append(req.thread_id, "user", query)
            self.store.append(req.thread_id, "assistant", payload.reply)
            entry["result"] = payload.model_dump()
        except Exception:
            entry["result"] = RequestResult(
                request_id=req.request_id, thread_id=req.thread_id, status="failed",
                reply="요청 처리에 실패했습니다. 같은 입력을 새 요청으로 다시 시도해 주세요.",
                metadata={"error_code": "REQUEST_FAILED", "original_query": query,
                          "selected_systems": selected}).model_dump()
        finally:
            if entry["result"] and entry["result"]["status"] in ("failed", "validation_failed"):
                pending = entry.get("selection_context")
                if pending:
                    self.store.save_pending(req.thread_id, pending)
                    entry["result"]["clarification"] = pending
            self.active.discard(req.thread_id)
            entry["changed"].set()

    async def stream(self, entry):
        seen = 0
        while True:
            entry["changed"].clear()
            for event in entry["events"][seen:]:
                yield event
            seen = len(entry["events"])
            if entry["result"] is not None:
                yield {"type": "FinalAnswer", **entry["result"]}
                break
            await entry["changed"].wait()
