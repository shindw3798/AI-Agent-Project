"""조사 그래프에서 사용하는 모델, MCP 클라이언트와 세션 저장소."""
from dataclasses import dataclass
from typing import Any


@dataclass
class SupervisorDeps:
    fast_llm: Any        # 분류·계획 모델
    quality_llm: Any     # 보고서 작성 모델
    mcp: Any             # MCP stdio 클라이언트
    session_store: Any   # SQLite 세션 저장소
    version: str         # 구현 버전
    monitoring_timeout: float = 15
    search_timeout: float = 45
    max_concurrency: int = 3
