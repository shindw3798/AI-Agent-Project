"""stdio MCP 연결을 공유하고 도구·리소스·프롬프트를 탐색한다.

flaky_first_tool_call은 첫 모니터링 호출의 타임아웃을 재현하는 테스트 옵션이다."""
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from langsmith import traceable
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_DIR = Path(__file__).parent.parent
DEFAULT_SERVER_SCRIPTS = {
    "ops": SERVER_DIR / "mcp_server.py",
    "docs": SERVER_DIR / "mcp_server_docs.py",
}


def _attr(obj, *names):
    """MCP SDK의 camelCase와 snake_case 필드명을 모두 지원한다."""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise AttributeError(f"{type(obj).__name__!r} has none of {names!r}")


class MCPClient:
    def __init__(self, flaky_first_tool_call: bool = False,
                 server_scripts: dict[str, Path] | None = None):
        self._server_scripts = server_scripts or DEFAULT_SERVER_SCRIPTS
        self._stack: AsyncExitStack | None = None
        self.sessions: dict[str, ClientSession] = {}
        self._lock = asyncio.Lock()
        self._flaky_pending = flaky_first_tool_call   # 첫 호출 실패 재현 옵션

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        try:
            for server_name, script in self._server_scripts.items():
                params = StdioServerParameters(
                    command=sys.executable, args=[str(script)],
                    env={**os.environ, "FASTMCP_LOG_LEVEL": "ERROR"})
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[server_name] = session
        except BaseException:
            # 두 번째 서버 연결이나 시작 취소가 실패해도 먼저 열린 연결을 남기지 않는다.
            await self.stop()
            raise

    async def stop(self) -> None:
        stack, self._stack = self._stack, None
        self.sessions = {}
        if stack is not None:
            # 정리 중 오류가 나도 객체는 닫힌 상태로 남겨 중복 종료를 막는다.
            await stack.aclose()

    @traceable(run_type="tool", name="mcp.call_tool")
    async def call_tool(self, name: str, arguments: dict, server: str = "ops") -> dict:
        if self._flaky_pending and name == "get_monitoring_status":
            self._flaky_pending = False
            raise asyncio.TimeoutError("simulated transient timeout")
        async with self._lock:
            result = await self.sessions[server].call_tool(name, arguments)
        if getattr(result, "isError", getattr(result, "is_error", False)):
            raise ValueError("MCP_TOOL_ERROR")
        return json.loads(result.content[0].text)

    async def find_tool(self, tool_name: str) -> str:
        for server, session in self.sessions.items():
            async with self._lock:
                result = await session.list_tools()
            if any(t.name == tool_name for t in result.tools):
                return server
        raise ValueError("MCP_TOOL_NOT_FOUND")

    @traceable(run_type="tool", name="mcp.list_resource_templates")
    async def list_resource_templates(self, server: str) -> list:
        async with self._lock:
            result = await self.sessions[server].list_resource_templates()
        return _attr(result, "resourceTemplates", "resource_templates")

    @traceable(run_type="tool", name="mcp.find_resource_template")
    async def find_resource_template(self, template_name: str) -> tuple[str, str]:
        """연결된 서버들을 순서대로 조회해 template_name을 제공하는 서버를 찾는다."""
        for server_name in self.sessions:
            templates = await self.list_resource_templates(server_name)
            matched = next((t for t in templates if t.name == template_name), None)
            if matched is not None:
                return server_name, _attr(matched, "uriTemplate", "uri_template")
        raise ValueError(
            f"MCP resource template not found on any connected server: {template_name}")

    @traceable(run_type="tool", name="mcp.read_resource")
    async def read_resource(self, uri: str, server: str) -> str:
        async with self._lock:
            result = await self.sessions[server].read_resource(uri)
        return result.contents[0].text

    @traceable(run_type="tool", name="mcp.list_prompts")
    async def list_prompts(self, server: str) -> list:
        async with self._lock:
            result = await self.sessions[server].list_prompts()
        return result.prompts

    @traceable(run_type="tool", name="mcp.find_prompt")
    async def find_prompt(self, prompt_name: str) -> str:
        """연결된 서버들을 순서대로 조회해 prompt_name을 제공하는 서버를 찾는다."""
        for server_name in self.sessions:
            prompts = await self.list_prompts(server_name)
            if any(p.name == prompt_name for p in prompts):
                return server_name
        raise ValueError(
            f"MCP prompt not found on any connected server: {prompt_name}")

    @traceable(run_type="tool", name="mcp.get_prompt")
    async def get_prompt(self, name: str, arguments: dict, server: str = "ops") -> str:
        async with self._lock:
            result = await self.sessions[server].get_prompt(name, arguments)
        return result.messages[0].content.text
