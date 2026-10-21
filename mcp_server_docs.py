"""복수 MCP 서버의 리소스 탐색을 위한 API 문서 예제 서버. 장애 진단에는 사용하지 않는다."""
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP(name="capstone-all-total-docs")

DOC_SECTIONS = {
    "billing": "# 내부 API 문서: billing\n\n결제 관련 엔드포인트와 webhook 재시도 정책을 설명합니다.",
    "auth": "# 내부 API 문서: auth\n\n토큰 발급/갱신 플로우와 스코프 목록을 설명합니다.",
}


@mcp.resource("docs://api-reference/{section}")
def get_api_doc(section: str) -> str:
    """내부 API 문서(section별 정적 문서)를 반환합니다. (agent는 사용하지 않음)"""
    if section not in DOC_SECTIONS:
        return f"# Doc Not Found\n\nNo API reference for section: {section}"
    return DOC_SECTIONS[section]


if __name__ == "__main__":
    try:
        mcp.run(show_banner=False)
    except TypeError:   # 구버전 fastmcp 호환
        mcp.run()
