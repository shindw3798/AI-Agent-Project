import json
from pathlib import Path
from contextlib import asynccontextmanager, AsyncExitStack
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", encoding="utf-8-sig")

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import ValidationError
from agent.deps import SupervisorDeps
from agent.factory import build_llms
from agent.graph import build_graph
from agent.mcp_client import MCPClient
from agent.session_store import SqliteSessionStore
from agent.service import ChatRequest, ChatService


@asynccontextmanager
async def lifespan(app):
    async with AsyncExitStack() as resources:
        mcp = MCPClient()
        # start 이전에 정리를 등록해야 모델 또는 DB 초기화 실패도 처리할 수 있다.
        resources.push_async_callback(mcp.stop)
        await mcp.start()
        store = SqliteSessionStore()
        resources.callback(store.close)
        backend, fast, quality = build_llms()
        deps = SupervisorDeps(fast, quality, mcp, store, "v5")
        service = ChatService(build_graph(deps), store)
        app.state.service = service
        app.state.models = {"backend": backend, "fast": fast.name, "quality": quality.name}
        try:
            yield
        finally:
            # 진행 중인 작업이 저장소나 MCP를 사용하지 않도록 먼저 취소하고 기다린다.
            tasks = [e["task"] for e in service.requests.values() if not e["task"].done()]
            for task in tasks:
                task.cancel()
            if tasks:
                import asyncio
                await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(ROOT / "web_chat.html")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "v5", **app.state.models}


@app.get("/session/{thread_id}/pending")
async def pending(thread_id: str):
    return {"clarification": app.state.service.store.pending(thread_id)}


@app.post("/chat")
async def chat(req: ChatRequest):
    entry = app.state.service.start(req)
    await entry["task"]
    return entry["result"]


@app.get("/chat/stream")
async def stream(message: str, thread_id: str, request_id: str,
                 clarification_id: str | None = None, selected_systems: str | None = None):
    try:
        req = ChatRequest(message=message, thread_id=thread_id, request_id=request_id,
                          clarification_id=clarification_id,
                          selected_systems=json.loads(selected_systems) if selected_systems else None)
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(422, "요청 또는 대상 선택 형식이 올바르지 않습니다.")
    entry = app.state.service.start(req)  # SSE를 열기 전에 요청 충돌을 HTTP 오류로 반환한다.
    async def generate():
        async for event in app.state.service.stream(entry):
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100)
