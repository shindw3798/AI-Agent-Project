"""관측 수치, 출처 연결과 원문 조치의 일치 여부를 검사한다."""
from pydantic import ValidationError
from .schemas import IncidentReport, ValidationResult
from .action_catalog import catalog


def validate(report, expected_facts, evidence, systems, limitations, attempt):
    errors = []
    def fail(code, path, message):
        errors.append({"code": code, "field_path": path, "message": message})
    try:
        parsed = IncidentReport.model_validate(report).model_dump()
    except (ValidationError, TypeError):
        return ValidationResult(status="failed", attempt=attempt, errors=[
            {"code": "INVALID_SCHEMA", "field_path": "report", "message": "보고서 스키마 불일치"}]).model_dump()
    if parsed["systems"] != systems or any(f["system_name"] not in systems for f in parsed["facts"]):
        fail("UNKNOWN_SYSTEM", "systems", "조사 대상 불일치")
    if parsed["facts"] != expected_facts:
        fail("FACT_MISMATCH", "facts", "관측 사실의 누락 또는 값·시스템·단위 변경")
    # 출처 ID가 존재하는 것과 올바른 대상의 근거인 것은 별개이므로 둘 다 확인한다.
    known = {e["evidence_id"]: e for e in evidence}
    fact_ids = {f["fact_id"] for f in expected_facts}
    fact_systems = {f["fact_id"]: f["system_name"] for f in expected_facts}
    used = set()
    source_actions = catalog(evidence)
    for field in ("hypotheses", "actions"):
        for i, item in enumerate(parsed[field]):
            ids = item["evidence_ids"]
            used.update(ids)
            if not ids:
                fail("MISSING_ACTION_SOURCE", f"{field}.{i}", "근거가 필요합니다")
            if any(e not in known for e in ids):
                fail("INVALID_REFERENCE", f"{field}.{i}", "조회하지 않은 근거")
            if field == "actions":
                if source_actions.get(item["source_step_id"]) != item:
                    fail("ACTION_SOURCE_MISMATCH", f"actions.{i}",
                         "조치·조건·대상·출처는 같은 원문 단계와 일치해야 합니다")
                if item["system_name"] not in systems:
                    fail("UNKNOWN_SYSTEM", f"{field}.{i}", "조사하지 않은 대상")
                if any(e in known and known[e]["system_name"] != item["system_name"] for e in ids):
                    fail("INVALID_REFERENCE", f"{field}.{i}", "다른 시스템의 검색 근거")
            elif not set(item["fact_ids"]) <= fact_ids:
                fail("FACT_MISMATCH", f"{field}.{i}", "존재하지 않는 사실 ID")
            else:
                cited_systems = {known[e]["system_name"] for e in ids if e in known}
                observed_systems = {fact_systems[f] for f in item["fact_ids"]}
                if len(observed_systems) != 1 or observed_systems != cited_systems:
                    fail("CROSS_SYSTEM_HYPOTHESIS", f"{field}.{i}",
                         "원인 후보는 같은 한 시스템의 관측과 문서로 뒷받침해야 합니다")
    covered = {a["system_name"] for a in parsed["actions"]}
    missing = {e["system_name"] for e in evidence} - covered
    if missing:
        fail("MISSING_GROUNDED_ACTION", "actions",
             "문서가 검색된 다음 시스템의 확인 절차 또는 조건부 권고가 누락되었습니다: " + ", ".join(sorted(missing)))
    if {r["evidence_id"] for r in parsed["references"]} != used or any(
            r != known.get(r["evidence_id"]) for r in parsed["references"]):
        fail("INVALID_REFERENCE", "references", "인용 목록 또는 원문 변경")
    if not set(limitations) <= set(parsed["limitations"]):
        fail("MISSING_LIMITATION", "limitations", "필수 한계 누락")
    return ValidationResult(status="failed" if errors else "passed", attempt=attempt, errors=errors).model_dump()
