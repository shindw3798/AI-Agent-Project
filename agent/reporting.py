from .schemas import Fact, IncidentReport


def facts_from(observations):
    facts = []
    for obs in observations:
        for name, value, unit in [
            ("status", obs["status"], ""), ("cpu_percent", obs["cpu_percent"], "%"),
            ("memory_percent", obs["memory_percent"], "%"),
            *[(f"alert_{i}", text, "") for i, text in enumerate(obs["alerts"])],
        ]:
            facts.append(Fact(fact_id=f"{obs['observation_id']}:{name}",
                              observation_id=obs["observation_id"], system_name=obs["system_name"],
                              metric_name=name, value=value, unit=unit).model_dump())
        if not obs["alerts"]:
            facts.append(Fact(fact_id=f"{obs['observation_id']}:alerts_empty",
                              observation_id=obs["observation_id"], system_name=obs["system_name"],
                              metric_name="alerts_empty", value="없음", unit="").model_dump())
    return facts


def assemble(systems, facts, narrative, evidence, limitations):
    cited = {eid for item in narrative.get("hypotheses", []) + narrative.get("actions", [])
             for eid in item.get("evidence_ids", [])}
    known = {e["evidence_id"]: e for e in evidence}
    hypotheses = []
    for item in narrative.get("hypotheses", []):
        # 모델이 고른 출처로 점검 가설을 구성하며 인과관계를 확정하지 않는다.
        
        titles = list(dict.fromkeys(known[e]["title"] for e in item["evidence_ids"] if e in known))
        text = ("점검 가설: " + " / ".join(titles)
                + "에 해당하는 문제가 있을 가능성이 있습니다. 관측만으로 원인을 확정할 수 없으며 추가 확인이 필요합니다.")
        hypotheses.append({**item, "text": text})
    return IncidentReport(
        systems=systems, facts=facts, hypotheses=hypotheses,
        actions=narrative.get("actions", []), limitations=limitations,
        references=[e for e in evidence if e["evidence_id"] in cited]).model_dump()


def render(report):
    lines = ["조사 대상: " + ", ".join(report["systems"]), "", "확인된 사실"]
    for fact in report["facts"]:
        label = {"status": "상태", "cpu_percent": "CPU", "memory_percent": "메모리"}.get(
            fact["metric_name"], "알림")
        lines.append(f"- {fact['system_name']} · {label}: {fact['value']}{fact['unit']} [{fact['fact_id']}]")
    if not report["facts"]:
        lines.append("- 확보된 관측 정보가 없습니다. 상태와 원인을 판단할 수 없습니다.")
    if len(report["systems"]) > 1:
        lines += ["", "대상 간 관계",
                  "- 현재 확보한 시스템별 관측과 런북만으로 공통 원인을 확정할 근거가 부족합니다. "
                  "같은 시간대의 변경 이력과 공통 의존성을 추가 확인해야 합니다."]
    lines += ["", "원인 후보 (확정 아님)"]
    lines += [f"- {h['text']} [근거: {', '.join(h['fact_ids'] + h['evidence_ids'])}]"
              for h in report["hypotheses"]] or ["- 추가 근거 없이 원인을 단정하지 않습니다."]
    lines += ["", "권장 조치 (실행하지 않음)"]
    lines += [f"- {a['system_name']} · 원문 단계 {a['source_step_id'].rsplit(':', 1)[-1]}: "
              f"{a['text']} [{', '.join(a['evidence_ids'])}]" for a in report["actions"]] or [
                  "- 근거가 충분하지 않으면 구체 조치를 제안하지 않습니다."]
    if report["actions"]:
        lines.append("- " + report["actions"][0]["prerequisites"])
    lines += ["", "한계 및 추가 확인"] + [f"- {x}" for x in report["limitations"]]
    lines += ["", "참고 문서"] + [
        f"- [{r['evidence_id']}] {r['title']} · {r['system_name']} · {r['document_id']}"
        for r in report["references"]]
    return "\n".join(lines)
