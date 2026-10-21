"""네트워크 호출 없이 고정 응답을 반환하는 테스트용 모델."""
import re

from langsmith import traceable


class FakeLLM:
    def __init__(self, name: str, usd_per_1k_tokens: float):
        self.name = name
        self.usd_per_1k = usd_per_1k_tokens

    def _usage(self, prompt: str, completion: str) -> dict:
        tokens = (len(prompt) + len(completion)) // 4 + 1
        return {"model": self.name, "tokens": tokens,
                "usd": round(tokens / 1000 * self.usd_per_1k, 6)}


class FastLLM(FakeLLM):
    """분류·계획용 고정 응답 모델."""

    @traceable(run_type="llm", name="fast_llm.route")
    def route(self, query: str) -> tuple[dict, dict]:
        """질문의 키워드로 대화와 장애 조사 경로를 구분한다."""
        incident = bool(re.search(r"장애|진단|다운|지연|급증|incident|티켓", query))
        result = {"route": "incident" if incident else "chat", "needs_analysis": False}
        return result, self._usage(query, str(result))

    @traceable(run_type="llm", name="fast_llm.plan")
    def plan(self, query: str, procedure_prompt: str) -> tuple[dict, dict]:
        """계획 검사를 위해 분석 작업이 불필요하게 포함된 응답을 재현한다."""
        systems = re.findall(r"[a-z][a-z0-9-]+-(?:service|\d+)", query) or ["auth-service"]
        if "web-01" not in systems:
            systems.append("web-01")   # 비교 조회 대상
        tasks = [
            {"id": f"task-{i}", "agent": "research", "kind": "monitoring",
             "target": s, "status": "pending"}
            for i, s in enumerate(dict.fromkeys(systems))
        ]
        tasks.append({"id": "task-analysis", "agent": "analysis",
                      "kind": "pattern-analysis", "status": "pending"})   # 자기모순 task
        result = {"needs_analysis": False, "tasks": tasks}
        return result, self._usage(query + procedure_prompt, str(result))

    @traceable(run_type="llm", name="fast_llm.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        """모니터링 결과에서 공통 패턴을 비교한다."""
        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        degraded = [d["system_name"] for d in mon if d.get("status") != "healthy"]
        if len(degraded) >= 2:
            summary = (f"{', '.join(degraded)} 복수 시스템이 동시에 저하됨 — "
                       f"공유 의존성 등 공통 원인이 의심됨")
        elif degraded:
            summary = f"{degraded[0]}만 단독 저하 — 연관 시스템은 정상, 국소 이슈로 추정"
        else:
            summary = "모니터링된 시스템 모두 healthy — 패턴상 이상 징후 없음"
        return summary, self._usage(str(collected), summary)

    @traceable(run_type="llm", name="fast_llm.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        """대화 이력에서 이름을 찾고 완성된 응답을 반환한다."""
        for msg in reversed(history):
            m = re.search(r"제 이름은 (\S+?)입니다", msg.get("content", ""))
            if m and re.search(r"이름이 뭐", query):
                reply = f"{m.group(1)}님이라고 말씀하셨어요. 무엇을 도와드릴까요?"
                if on_token is not None:
                    on_token(reply)
                return reply, self._usage(query, reply)
        m = re.search(r"제 이름은 (\S+?)입니다", query)
        if m:
            reply = f"안녕하세요, {m.group(1)}님! 무엇을 도와드릴까요?"
        else:
            reply = "안녕하세요! 무엇을 도와드릴까요?"
        if on_token is not None:
            on_token(reply)
        return reply, self._usage(query, reply)


class QualityLLM(FakeLLM):
    """보고서 작성용 고정 응답 모델."""

    @traceable(run_type="llm", name="quality_llm.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
              feedback: str | None, on_token=None) -> tuple[str, dict]:
        """재작성 검사를 위해 첫 초안은 짧게, 피드백 후에는 보고서를 반환한다."""
        if attempt == 1:
            draft = "재시작하면 됩니다."
            if on_token is not None:
                on_token(draft)
            return draft, self._usage(query, draft)

        mon = [c for c in collected if c["source"] == "monitoring"]
        run = [c for c in collected if c["source"] == "runbook"]
        summary = " · ".join(
            f"{c['data']['system_name']} 상태={c['data']['status']}, "
            f"CPU {c['data']['cpu_percent']}% (알림 {len(c['data']['alerts'])}건)"
            for c in mon)
        lines = [f"'{query}' 진단 결과:", f"1) 모니터링: {summary}"]
        if run:
            body_lines = [l.strip() for l in run[0]["data"].strip().splitlines() if l.strip()]
            title = body_lines[0].lstrip("# ")
            steps = [m.group(1) for l in body_lines[1:]
                     if (m := re.match(r"^\d+\.\s+(.*)", l))]
            advice = " ".join(steps[1:3]) or "표준 절차를 따라 조치하세요."
            lines.append(f"2) 표준 절차: '{title}'에 따라 {advice}")
        lines.append("3) 정상화 확인 후 원인 분석과 재발 방지 대책을 문서화하세요.")
        draft = " ".join(lines)
        if on_token is not None:
            on_token(draft)
        return draft, self._usage(query + str(feedback), draft)
