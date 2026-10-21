"""런북의 조건과 조치를 원문 단계 단위로 연결한다."""
import re

REVIEW_GATE = "원문 단계에 명시된 조건과 선행 절차를 확인한 뒤 담당자가 적용 여부를 판단하세요. 실제 조치는 실행하지 않았습니다."


class ActionSelectionError(ValueError):
    def __init__(self, path, message):
        super().__init__(message)
        self.path = path


def catalog(evidence):
    result = {}
    for e in evidence:
        # 여러 줄로 된 단계는 보존하고 중복 단계 번호는 거부한다.
        blocks, current = [], None
        for line in e["text"].splitlines():
            match = re.match(r"^\s*(\d+)\.\s+(.+)$", line)
            if match:
                current = [int(match[1]), match[2]]
                blocks.append(current)
            elif line.strip() and not line.lstrip().startswith("#") and current is not None:
                current[1] += "\n" + line.strip()
        if not blocks:
            body = "\n".join(line for line in e["text"].splitlines()
                             if not line.lstrip().startswith("#")).strip()
            blocks = [(1, body)] if body else []
        for number, text in blocks:
            step_id = f"{e['evidence_id']}:step:{number}"
            if step_id in result:
                raise ActionSelectionError("actions", "중복된 원문 단계 ID입니다")
            result[step_id] = {
                "source_step_id": step_id,
                "system_name": e["system_name"],
                "evidence_ids": [e["evidence_id"]],
                "text": text,
                "prerequisites": REVIEW_GATE,
            }
    return result


def materialize(selection, evidence):
    available = catalog(evidence)
    selected = []
    for i, item in enumerate(selection["actions"]):
        step_id = item["source_step_id"]
        if step_id not in available:
            raise ActionSelectionError(f"actions.{i}.source_step_id",
                                       "제공된 action_catalog에 없는 단계입니다. 목록의 ID만 선택하세요.")
        if step_id in selected:
            raise ActionSelectionError(f"actions.{i}.source_step_id", "같은 단계를 중복 선택하지 마세요.")
        selected.append(step_id)
    # 모델의 선택 순서와 관계없이 원문 순서로 정렬한다.
    return {"hypotheses": selection["hypotheses"],
            "actions": [dict(value) for key, value in available.items() if key in selected]}
