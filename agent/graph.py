"""대상 확인, 조사, 보고서 작성과 검증을 연결하는 그래프."""
import asyncio
import re
import uuid
from functools import partial
from typing import TypedDict
from langgraph.graph import StateGraph, END
from langsmith import traceable
from pydantic import ValidationError
from .targets import resolve, KNOWN_SYSTEMS
from .schemas import NarrativeSelection
from .action_catalog import materialize, ActionSelectionError
from .nodes.investigation import run_task
from .reporting import facts_from, assemble, render
from .validation import validate


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    request_id: str
    history: list
    selected_systems: list
    route: str
    targets: list
    clarification: dict
    tasks: list
    task_results: list
    observations: list
    evidence: list
    facts: list
    limitations: list
    report: dict
    validation: dict
    validation_attempts: int
    validation_feedback: list
    previous_draft: dict
    validation_history: list
    status: str
    final: str
    events: list
    cost_usd: float
    needs_analysis: bool
    replan_rounds: int
    retry_count: int


def events(state, *items):
    return state.get("events", []) + list(items)


@traceable(name="resolve_targets")
async def route(state, deps):
    # 대상 선택을 마친 요청은 다시 모델에 맡기지 않고 선택한 시스템으로 진행한다.
    selected = state.get("selected_systems")
    decision, usage = await asyncio.to_thread(deps.fast_llm.route, state["query"]) if not selected else (
        {"route": "incident", "needs_analysis": False}, {"usd": 0})
    resolution = resolve(state["query"])
    operational = bool(re.search(r"장애|진단|점검|상태|느리|느려|지연|사용률|저하|공간|메모리|CPU", state["query"], re.I))
    incident = bool(selected) or decision["route"] == "incident" or operational
    if not incident:
        return {"route": "chat", "cost_usd": usage["usd"],
                "events": events(state, {"type": "RouteDecided", "route": "chat"})}
    if selected:
        if not set(selected) <= set(KNOWN_SYSTEMS):
            raise ValueError("INVALID_SELECTION")
        resolution.resolved_systems = sorted(set(selected))
        resolution.unresolved_names = []
        resolution.status = "resolved"
    if resolution.status == "needs_clarification":
        resolution.clarification_id = uuid.uuid4().hex
        clarification = resolution.model_dump() | {"options": KNOWN_SYSTEMS, "original_query": state["query"]}
        return {"route": "incident", "status": "needs_clarification", "clarification": clarification,
                "final": "어떤 시스템을 조사할까요? 등록된 시스템을 선택해 주세요."
                         + (f" 미등록 이름: {', '.join(resolution.unresolved_names)}" if resolution.unresolved_names else ""),
                "cost_usd": usage["usd"], "events": events(state, {"type": "ClarificationRequired", **clarification})}
    return {"route": "incident", "targets": resolution.resolved_systems,
            "needs_analysis": decision.get("needs_analysis", False), "cost_usd": usage["usd"],
            "events": events(state, {"type": "TargetResolved", "systems": resolution.resolved_systems})}


async def chat(state, deps):
    reply, usage = await deps.fast_llm.chat(state["query"], state.get("history", []))
    return {"final": reply, "status": "completed", "cost_usd": state.get("cost_usd", 0)+usage["usd"]}


@traceable(name="plan_and_gate")
async def plan(state, deps):
    try:
        server = await deps.mcp.find_prompt("incident_analysis_prompt")
        procedure = await deps.mcp.get_prompt("incident_analysis_prompt",
                    {"system_name": ", ".join(state["targets"]), "symptom": state["query"]}, server=server)
        result, usage = await asyncio.to_thread(deps.fast_llm.plan, state["query"], procedure)
        needs = bool(result.get("needs_analysis"))
    except Exception:
        needs, usage = False, {"usd": 0}
    tasks = [{"id": f"monitoring:{s}", "target": s, "kind": "monitoring", "status": "pending"}
             for s in state["targets"]]
    return {"tasks": tasks, "needs_analysis": needs, "cost_usd": state.get("cost_usd", 0)+usage["usd"],
            "events": events(state, {"type": "PlanCreated", "targets": state["targets"], "task_count": len(tasks)})}


@traceable(name="monitoring_isolated")
async def monitor(state, deps):
    sem = asyncio.Semaphore(deps.max_concurrency)
    async def one(system):
        return await run_task(system, "monitoring",
            lambda: deps.mcp.call_tool("get_monitoring_status", {"system_name": system}),
            sem, deps.monitoring_timeout)
    results = await asyncio.gather(*(one(s) for s in state["targets"]))
    observations = [r["data"] for r in results if r["status"] == "success"]
    limits = [f"{r['system_name']}: 모니터링 실패({r['error_code']}); 상태와 원인을 판단할 수 없습니다."
              for r in results if r["status"] != "success"]
    return {"observations": observations, "task_results": results, "limitations": limits,
            "facts": facts_from(observations), "retry_count": sum(r["retry_count"] for r in results),
            "events": events(state, *[{"type": "MonitoringCompleted" if r["status"] == "success" else "TaskFailed",
                                      "system_name": r["system_name"], "status": r["status"],
                                      "error_code": r["error_code"]} for r in results])}


@traceable(name="retrieve_runbooks")
async def retrieve(state, deps):
    sem = asyncio.Semaphore(deps.max_concurrency)
    async def one(obs):
        async def search():
            server = await deps.mcp.find_tool("search_runbooks")
            args = {
                "query": state["query"], "system_name": obs["system_name"],
                "monitoring_summary": obs, "top_k": 3}
            result = await deps.mcp.call_tool("search_runbooks", args, server=server)
            # 해당 시스템의 관측 증상으로 검색을 한 번만 보완한다.
            if result.get("status") == "no_match" and obs["alerts"]:
                args["query"] = "; ".join(obs["alerts"])
                result = await deps.mcp.call_tool("search_runbooks", args, server=server)
                result["supplement_attempted"] = True
            return result
        return await run_task(obs["system_name"], "search", search, sem, deps.search_timeout)
    results = await asyncio.gather(*(one(o) for o in state["observations"]))
    evidence = [e for r in results if r["status"] == "success" for e in r["data"]["evidence"]]
    limits = list(state["limitations"])
    healthy = {o["system_name"] for o in state["observations"] if o["status"] == "healthy" and not o["alerts"]}
    for r in results:
        if r["status"] == "failed":
            limits.append(f"{r['system_name']}: 런북 검색 실패; 관측 사실만 제공하고 구체 조치를 생략합니다.")
        elif r["status"] == "no_match" and r["system_name"] not in healthy:
            limits.append(f"{r['system_name']}: 관련 런북 없음; 추가 근거 수집이 필요합니다.")
    limits += ["모니터링은 실습용 시뮬레이션입니다. 실제 조치는 실행하지 않았습니다.",
               "규칙 검증은 수치와 출처 연결을 검사하며 원인·권고의 의미적 정확성 전체를 보장하지 않습니다."]
    return {"evidence": evidence, "limitations": limits, "task_results": state["task_results"]+results,
            "replan_rounds": int(any((r["data"] or {}).get("supplement_attempted") for r in results)),
            "retry_count": state["retry_count"]+sum(r["retry_count"] for r in results),
            "events": events(state, *[{"type": "RunbooksRetrieved", "system_name": r["system_name"],
                                      "status": r["status"], "evidence": (r["data"] or {}).get("evidence", [])}
                                     for r in results])}


@traceable(name="write_grounded_report")
async def write(state, deps):
    # 모델은 문서의 단계 ID를 고른다. 조건과 조치 문장은 서버가 원문에서 가져온다.
    attempt = state.get("validation_attempts", 0)+1
    usage = {"usd": 0}
    narrative = None
    try:
        if state["evidence"]:
            narrative, usage = await deps.quality_llm.report(
                state["query"], state["facts"], state["evidence"], state.get("validation_feedback"),
                state.get("previous_draft"))
            selection = NarrativeSelection.model_validate(narrative).model_dump()
            rendered = materialize(selection, state["evidence"])
        else:
            narrative, usage = {"hypotheses": [], "actions": []}, {"usd": 0}
            rendered = narrative
        report = assemble(state["targets"], state["facts"], rendered, state["evidence"], state["limitations"])
        verdict = validate(report, state["facts"], state["evidence"], state["targets"], state["limitations"], attempt)
    except ActionSelectionError as exc:
        report = {}
        verdict = {"status": "failed", "attempt": attempt, "errors": [
            {"code": "INVALID_ACTION_STEP", "field_path": exc.path, "message": str(exc)}]}
    except ValidationError as exc:
        report = {}
        verdict = {"status": "failed", "attempt": attempt, "errors": [
            {"code": "INVALID_SCHEMA", "field_path": ".".join(str(x) for x in err["loc"]),
             "message": err["msg"] + " — 근거가 없는 후보/조치는 해당 항목을 생략하세요."}
            for err in exc.errors(include_input=False, include_url=False)]}
    except Exception as exc:
        report = {}
        verdict = {"status": "failed", "attempt": attempt, "errors": [
            {"code": "INVALID_SCHEMA", "field_path": "narrative", "message": "작성 실패 또는 JSON 스키마 오류",
             "error_type": type(exc).__name__}]}
    return {"report": report, "validation": verdict, "validation_attempts": attempt,
            "previous_draft": narrative if isinstance(narrative, dict) else None,
            "validation_history": state.get("validation_history", []) + [verdict],
            "validation_feedback": verdict["errors"], "cost_usd": state.get("cost_usd", 0)+usage["usd"],
            "events": events(state, {"type": "ValidationCompleted", **verdict})}


async def finish(state, deps):
    # 부분 실패도 확보한 사실은 전달한다. 검증이 끝내 실패한 초안은 공개하지 않는다.
    report = state["report"]
    if not state["observations"]:
        status = "failed"
    elif state["validation"]["status"] != "passed":
        status = "validation_failed"
    else:
        status = "partial" if len(state["limitations"]) > 2 else "completed"
    if state["validation"]["status"] != "passed":
        report = assemble(state["targets"], state["facts"], {"hypotheses": [], "actions": []}, [],
                          state["limitations"]+["보고서 검증에 실패하여 확인된 사실만 표시합니다."])
    return {"status": status, "report": report, "final": render(report)}


def build_graph(deps):
    g = StateGraph(AgentState)
    for name, fn in [("route", route), ("chat", chat), ("plan", plan), ("monitor", monitor),
                     ("retrieve", retrieve), ("write", write), ("finish", finish)]:
        g.add_node(name, partial(fn, deps=deps))
    g.set_entry_point("route")
    g.add_conditional_edges("route", lambda s: END if s.get("status") == "needs_clarification"
                            else "chat" if s["route"] == "chat" else "plan")
    g.add_edge("chat", END)
    g.add_edge("plan", "monitor")
    g.add_edge("monitor", "retrieve")
    g.add_edge("retrieve", "write")
    g.add_conditional_edges("write", lambda s: "write" if s["validation"]["status"] == "failed"
                            and s["validation_attempts"] < 3 else "finish")
    g.add_edge("finish", END)
    return g.compile()
