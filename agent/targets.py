import json
import re
from datetime import date
from pathlib import Path
from .schemas import TargetResolution

KNOWN_SYSTEMS = sorted(json.loads(
    (Path(__file__).resolve().parent.parent / "data/monitoring.json").read_text(encoding="utf-8")))


def _is_calendar_date(token):
    # 질문에 적힌 날짜는 대상 이름이 아니다. 실제로 존재하는 날짜만 제외한다.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
        return False
    try:
        date.fromisoformat(token)
        return True
    except ValueError:
        return False


def resolve(query):
    query = query.lower()
    known = [s for s in KNOWN_SYSTEMS
             if re.search(r"(?<![a-z0-9_-])" + re.escape(s) + r"(?![a-z0-9_-])", query)]
    names = re.findall(r"(?<![a-z0-9_-])[a-z0-9]+(?:[-_][a-z0-9]+)+(?![a-z0-9_-])", query)
    names = [name for name in names if not _is_calendar_date(name)]
    unknown = sorted(set(names) - set(KNOWN_SYSTEMS))
    return TargetResolution(
        status="resolved" if known and not unknown else "needs_clarification",
        requested_names=sorted(set(names)), resolved_systems=known, unresolved_names=unknown)
