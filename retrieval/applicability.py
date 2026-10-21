"""관측된 증상이 런북의 적용 조건을 만족하는지 확인한다.

유사도 검색에 앞서 알려진 알림 표현을 검사하며, 새 표현은 누락될 수 있다."""
import re

PREREQUISITES = {
    "cpu-spike": r"\bcpu\b.*(?:threshold|high|spike|[89]\d\s*%|100\s*%)|CPU.*(?:급증|높|초과)",
    "memory-leak": r"memory.*(?:climb|increas|leak|high)|heap.*(?:limit|full)|메모리.*(?:증가|누수|높)|힙.*한계",
    "disk-full": r"disk.*(?:space low|full|usage|capacity)|디스크.*(?:부족|용량|가득)",
    "high-latency": r"latency|responding slow|connection pool.*(?:used|exhaust)|응답.*(?:지연|느)|커넥션 풀",
    "network-timeout": r"upstream timeout|network.*(?:timeout|packet loss)|네트워크.*(?:타임아웃|손실)",
    "db-down": r"\bdatabase (?:is )?down\b|\bprimary unreachable\b|\bDB.*(?:다운|중단)|데이터베이스.*(?:중단|다운)",
}

def applicable(document_id, alerts):
    pattern = PREREQUISITES.get(document_id)
    return bool(pattern and any(re.search(pattern, alert, re.I) for alert in alerts))
