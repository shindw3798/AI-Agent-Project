"""로컬 Ollama 모델 어댑터. 분류·계획과 보고서 작성 모델을 분리한다."""
import json
import re
from pathlib import Path

import httpx
from langsmith import traceable

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"


from .targets import KNOWN_SYSTEMS


def _guess_target(query: str) -> str:
    """질문에 명시된 첫 시스템을 반환한다. 대상이 없으면 빈 문자열을 반환한다."""
    matches = [(query.index(s), s) for s in KNOWN_SYSTEMS if s in query]
    return min(matches)[1] if matches else ""


def _guess_targets(query: str) -> list[str]:
    """질문에 명시된 모든 시스템을 복구한다."""
    matched = sorted(s for s in KNOWN_SYSTEMS if s in query)
    return matched


ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ["chat", "incident"]},
        "needs_analysis": {"type": "boolean"},
    },
    "required": ["route", "needs_analysis"],
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_analysis": {"type": "boolean"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string",
                              "enum": ["research", "analysis"]},
                    "target": {"type": "string"},
                },
                "required": ["agent"],
            },
        },
    },
    "required": ["needs_analysis", "tasks"],
}


class OllamaLLM:
    def __init__(self, model: str, usd_per_1k_tokens: float):
        self.name = f"ollama:{model}"
        self.model = model
        self.usd_per_1k = usd_per_1k_tokens

    def _chat(self, system: str, user: str, schema: dict | None = None) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": 0.0},
        }
        if schema is not None:
            payload["format"] = schema
        resp = httpx.post(OLLAMA_URL, json=payload, timeout=300.0)
        resp.raise_for_status()
        data = resp.json()
        content = data["message"]["content"]
        tokens = (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0)
        usage = {"model": self.name, "tokens": tokens,
                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        return content, usage

    async def _chat_stream(self, system: str, user: str, on_token=None) -> tuple[str, dict]:
        """Ollama NDJSON 응답 조각을 on_token 콜백으로 전달한다."""
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": True,
            "options": {"temperature": 0.0},
        }
        chunks: list[str] = []
        usage = {"model": self.name, "tokens": 0, "usd": 0.0}
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream("POST", OLLAMA_URL, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    piece = data.get("message", {}).get("content", "")
                    if piece:
                        chunks.append(piece)
                        if on_token is not None:
                            on_token(piece)
                    if data.get("done"):
                        tokens = ((data.get("prompt_eval_count") or 0)
                                  + (data.get("eval_count") or 0))
                        usage = {"model": self.name, "tokens": tokens,
                                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        usage["prompt"] = f"[SYSTEM]\n{system}\n\n[USER]\n{user}"
        return "".join(chunks), usage


class OllamaFastLLM(OllamaLLM):
    """분류·계획·단순 대화 전담 (저사양 소형 모델)."""

    @traceable(run_type="llm", name="ollama_fast.route")
    def route(self, query: str) -> tuple[dict, dict]:
        system = ("당신은 ICT 운영 지원 라우터다. 사용자 요청을 분류하라. "
                  "장애·성능 저하·지연·진단·모니터링·티켓 등 운영 이슈면 route=incident, "
                  "인사·잡담·일반 질문이면 route=chat. "
                  "needs_analysis는 패턴 비교/통계 분석이 꼭 필요할 때만 true.")
        content, usage = self._chat(system, query, schema=ROUTE_SCHEMA)
        try:
            raw = json.loads(content)
            route = raw.get("route") if raw.get("route") in ("chat", "incident") else None
            result = {"route": route or "chat",
                      "needs_analysis": bool(raw.get("needs_analysis", False))}
        except (json.JSONDecodeError, AttributeError):
            # 분류 응답을 파싱할 수 없으면 키워드로 경로를 결정한다.
            incident = bool(re.search(r"장애|진단|다운|지연|급증|incident|티켓", query))
            result = {"route": "incident" if incident else "chat",
                      "needs_analysis": False}
        return result, usage

    @traceable(run_type="llm", name="ollama_fast.plan")
    def plan(self, query: str, procedure_prompt: str) -> tuple[dict, dict]:
        system = ("당신은 ICT 운영 Supervisor의 planning 노드다. 표준 진단 절차에 따라 "
                  "실행 계획(tasks)을 JSON으로 세워라. "
                  "사용자 요청에 등장한 시스템마다 agent=research task를 하나씩 세워라 "
                  f"(알려진 시스템: {', '.join(sorted(KNOWN_SYSTEMS))} — 특정 시스템이 "
                  "언급되지 않았으면 조사 대상을 만들지 마라). "
                  "패턴 분석이 꼭 필요하다고 판단되면 agent=analysis task를 추가해도 된다.")
        user = f"## 표준 절차\n{procedure_prompt}\n\n## 사용자 요청\n{query}"
        content, usage = self._chat(system, user, schema=PLAN_SCHEMA)

        # 대상 이름과 누락·중복 작업을 정규화한다.
        
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            raw = {"needs_analysis": False, "tasks": []}
        tasks, seen, idx = [], set(), 0
        analysis_added = False
        for t in raw.get("tasks", []):
            agent = t.get("agent")
            if agent == "research":
                target = t.get("target", "")
                target = target if target in KNOWN_SYSTEMS else _guess_target(query)
                if target in seen:
                    continue
                seen.add(target)
                tasks.append({"id": f"task-{idx}", "agent": "research",
                              "kind": "monitoring", "target": target,
                              "status": "pending"})
                idx += 1
            elif agent == "analysis" and not analysis_added:
                analysis_added = True
                tasks.append({"id": "task-analysis", "agent": "analysis",
                              "kind": "pattern-analysis", "status": "pending"})
        if not seen:   # 조사 작업이 없으면 질문에 명시된 모든 대상을 복구한다.
            
            
            recovered = [{"id": f"task-{i}", "agent": "research",
                          "kind": "monitoring", "target": target,
                          "status": "pending"}
                         for i, target in enumerate(_guess_targets(query))]
            tasks = recovered + tasks
        result = {"needs_analysis": bool(raw.get("needs_analysis", False)),
                  "tasks": tasks}
        return result, usage

    @traceable(run_type="llm", name="ollama_fast.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        system = ("당신은 ICT 운영 패턴 분석가다. 여러 시스템의 모니터링 결과를 비교해 "
                  "공통 원인이 의심되는 패턴이 있는지 한두 문장으로 짧게 답하라. "
                  "패턴이 없으면 '특이 패턴 없음'이라고만 답하라.")
        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        facts = "\n".join(
            f"- {d['system_name']}: 상태={d['status']}, CPU {d['cpu_percent']}%, "
            f"알림 {d['alerts']}" for d in mon) or "(모니터링 데이터 없음)"
        return self._chat(system, facts)

    @traceable(run_type="llm", name="ollama_fast.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        system = ("당신은 간결한 한국어 비서다. 한두 문장으로 답하라. "
                  "대화 이력에서 사용자가 밝힌 이름이 있으면 정확히 기억해 답하라.")
        rendered = "\n".join(f"{m['role']}: {m['content']}" for m in history[-10:])
        user = (f"## 지금까지의 대화\n{rendered or '(없음)'}\n\n"
                f"## 사용자의 새 메시지\n{query}")
        return await self._chat_stream(system, user, on_token=on_token)


class OllamaQualityLLM(OllamaLLM):
    """최종 답변 작성 전담 (상대적으로 큰 모델)."""

    @traceable(run_type="llm", name="ollama_quality.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
              feedback: str | None, on_token=None) -> tuple[str, dict]:
        system = ("당신은 ICT 운영 보고 작성자다. 수집된 사실만 사용해 한국어로 "
                  "간결한 대응 보고를 작성하라. 반드시 '1) ... 2) ... 3) ...' 형식의 "
                  "번호 절차로 답하고, 수치는 수집된 값을 그대로 인용하라.")
        facts = []
        for c in collected:
            if c["source"] == "monitoring":
                d = c["data"]
                facts.append(f"- 모니터링 {d['system_name']}: 상태={d['status']}, "
                             f"CPU {d['cpu_percent']}%, 메모리 {d['memory_percent']}%, "
                             f"알림 {len(d['alerts'])}건 {d['alerts']}")
            elif c["source"] == "runbook":
                facts.append(f"- runbook 발췌:\n{c['data'].strip()}")
            elif c["source"] == "analysis":
                facts.append(f"- 패턴 분석: {c['data'].strip()}")
        user = f"## 사용자 요청\n{query}\n\n## 수집된 사실\n" + "\n".join(facts)
        if feedback:
            user += f"\n\n## 이전 초안에 대한 검증 반려 사유 (반드시 반영)\n{feedback}"
        return await self._chat_stream(system, user, on_token=on_token)
