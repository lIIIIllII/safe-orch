"""Domain 엔티티 (설계서 §5.1 중 부록 A.2 범위).

app 내부 모듈을 import하지 않는다(부록 A.1). 시간은 Horizon 원점 기준 정수 분, 점유는 [start, end).
조회 결과는 매번 새 객체로 만든다. 모델은 frozen이다.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["UNIT_PLANNER", "REPORTER", "SUPERVISOR"]
Relation = Literal["SAME", "ADJACENT", "BELOW"]
StoredRelation = Literal["ADJACENT", "BELOW"]
Lifecycle = Literal["DRAFT", "NEEDS_INFO", "READY"]
FieldStatus = Literal["PROPOSED", "CONFIRMED"]
CandidateKind = Literal["REPLAN", "RECONFIRM"]
ValidationStatus = Literal["PASS", "FAIL", "INCOMPLETE"]
ScopeLevel = Literal["L0", "L1", "L2"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ── Pack 정의 ──────────────────────────────────────────────────


class WorkType(Frozen):
    work_type: str
    hazard_tags: tuple[str, ...] = Field(min_length=1)
    critical_fields: tuple[str, ...]


class Rule(Frozen):
    rule_id: str
    type: Literal["SEPARATION", "CAPACITY"]
    hazard_a: str | None = None
    hazard_b: str | None = None
    relations: tuple[Relation, ...] = ()
    min_gap: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _separation_fields(self) -> "Rule":
        if self.type == "SEPARATION" and not (self.hazard_a and self.hazard_b and self.relations):
            raise ValueError("SEPARATION needs hazard_a, hazard_b, relations")
        return self


# ── 현장 사실 ──────────────────────────────────────────────────


class Site(Frozen):
    site_id: str
    pack_hash: str
    horizon_start_utc: str
    horizon_minutes: int = Field(gt=0)
    context_version: int = Field(ge=0)
    plan_revision: int = Field(ge=0)


class WorkUnit(Frozen):
    unit_id: str
    name: str
    unit_type: str


class Actor(Frozen):
    actor_id: str
    name: str
    unit_id: str
    roles: tuple[Role, ...]


class Zone(Frozen):
    zone_id: str


class ZoneRelation(Frozen):
    zone_a: str
    zone_b: str
    relation: StoredRelation


class Resource(Frozen):
    resource_id: str
    resource_type: str
    owner_unit_id: str
    allowed_unit_ids: tuple[str, ...]
    capacity: Literal[1] = 1
    available_intervals: tuple[tuple[int, int], ...]


class Predecessor(Frozen):
    task_id: str
    min_lag: int = 0


class Movable(Frozen):
    time: bool
    resource: bool


class FieldRecord(Frozen):
    """critical field 하나의 확인 기록 (부록 A.8)."""

    value: Any
    status: FieldStatus
    source_ref: str


class Task(Frozen):
    task_id: str
    revision: int = Field(ge=1)
    unit_id: str
    owner_actor_id: str
    work_type: str
    hazard_tags: tuple[str, ...]  # 서버가 Pack에서 도출 (I-14)
    zone_id: str
    duration: int = Field(gt=0)
    earliest_start: int
    latest_start: int
    latest_end: int
    required_resource_type: str | None = None
    requested_resource_id: str | None = None
    predecessors: tuple[Predecessor, ...] = ()
    movable: Movable
    fields: dict[str, FieldRecord]
    lifecycle: Lifecycle

    @model_validator(mode="after")
    def _window(self) -> "Task":
        if self.earliest_start > self.latest_start:
            raise ValueError("earliest_start > latest_start")
        if self.earliest_start + self.duration > self.latest_end:
            raise ValueError("earliest_start + duration > latest_end")
        return self


class Assignment(Frozen):
    task_id: str
    start: int
    end: int
    resource_id: str | None = None

    @model_validator(mode="after")
    def _interval(self) -> "Assignment":
        if self.start >= self.end:
            raise ValueError("start must be < end")
        return self


class Plan(Frozen):
    plan_revision: int = Field(ge=0)
    assignments: tuple[Assignment, ...]
    candidate_id: str | None
    committed_context_version: int = Field(ge=0)


# ── 계산·검증 기록 (불변) ──────────────────────────────────────


class Snapshot(Frozen):
    snapshot_id: str
    snapshot_hash: str
    content: dict[str, Any]


class SearchSpec(Frozen):
    search_spec_id: str
    hash: str
    snapshot_id: str
    acting_unit_id: str
    scope_level: ScopeLevel
    axes: dict[str, Movable]
    resource_alternatives: dict[str, tuple[str, ...]]
    time_limit_s: int = Field(gt=0)


class SolverResult(Frozen):
    solver_result_id: str
    search_spec_id: str
    stage1: dict[str, Any]
    stage2: dict[str, Any] | None
    chosen_stage: Literal[1, 2] | None


class Candidate(Frozen):
    candidate_id: str
    snapshot_id: str
    search_spec_id: str | None
    search_spec_hash: str | None
    solver_result_id: str | None
    base_plan_revision: int = Field(ge=0)
    context_version: int = Field(ge=0)
    pack_hash: str
    assignments: tuple[Assignment, ...]
    candidate_hash: str
    kind: CandidateKind


class ValidationCheck(Frozen):
    check_id: str
    status: str
    task_ids: tuple[str, ...] = ()
    reason_code: str | None = None


class Validation(Frozen):
    validation_id: str
    candidate_id: str
    status: ValidationStatus
    checks: tuple[ValidationCheck, ...]
