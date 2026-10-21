"""요청, 조사 결과와 보고서의 데이터 구조 및 필드 검증."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TargetResolution(Contract):
    status: Literal["resolved", "needs_clarification"]
    requested_names: list[str]
    resolved_systems: list[str]
    unresolved_names: list[str]
    clarification_id: str | None = None


class Observation(Contract):
    observation_id: str
    system_name: str
    status: str
    cpu_percent: float
    memory_percent: float
    alerts: list[str]
    observed_at: str


class TaskResult(Contract):
    task_id: str
    system_name: str
    kind: Literal["monitoring", "search"]
    status: Literal["success", "failed", "no_match"]
    data: dict | None = None
    error_code: str | None = None
    retry_count: int = 0
    elapsed_ms: float = 0


class RetrievedEvidence(Contract):
    evidence_id: str
    document_id: str
    chunk_id: str
    title: str
    incident_type: str
    text: str
    score: float
    source_version: str
    system_name: str


class Fact(Contract):
    fact_id: str
    observation_id: str
    system_name: str
    metric_name: str
    value: float | str
    unit: str


class Hypothesis(Contract):
    text: str = Field(min_length=1)
    fact_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class RecommendedAction(Contract):
    source_step_id: str = ""
    text: str = Field(min_length=1)
    system_name: str
    evidence_ids: list[str] = Field(min_length=1)
    prerequisites: str = Field(min_length=1)


class Narrative(Contract):
    hypotheses: list[Hypothesis]
    actions: list[RecommendedAction]


class ActionChoice(Contract):
    source_step_id: str = Field(min_length=1)


class NarrativeSelection(Contract):
    hypotheses: list[Hypothesis]
    actions: list[ActionChoice]


class IncidentReport(Contract):
    systems: list[str]
    facts: list[Fact]
    hypotheses: list[Hypothesis]
    actions: list[RecommendedAction]
    limitations: list[str]
    references: list[RetrievedEvidence]


class ValidationResult(Contract):
    status: Literal["not_run", "passed", "failed"] = "not_run"
    attempt: int = 0
    errors: list[dict] = Field(default_factory=list)


class RequestResult(Contract):
    request_id: str
    thread_id: str
    status: Literal["completed", "needs_clarification", "partial", "validation_failed", "failed"]
    reply: str
    report: IncidentReport | None = None
    clarification: dict | None = None
    validation: ValidationResult = Field(default_factory=ValidationResult)
    task_results: list[TaskResult] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
