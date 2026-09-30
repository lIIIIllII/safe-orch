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
Axis = Literal["TIME", "RESOURCE"]


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


class FeedbackConstraint(Frozen):
    """확인된 작업·축 고정 (§5.1, §9.2, 부록 A.14). task revision에 묶지 않는다(I-10)."""

    constraint_id: str
    task_id: str
    frozen_axes: tuple[Axis, ...]
    source_type: Literal["DECISION", "PROPOSAL"]
    source_id: str


class HoldRef(Frozen):
    """Snapshot에 넣는 ACTIVE Hold (§10, 부록 A.14)."""

    hold_id: str
    scope: Literal["TASK", "SITE"]
    task_id: str | None = None


class Consent(Frozen):
    """작업 담당자의 이동 동의 (§5.1, §9.3). scope는 TIME {start_min, start_max},
    RESOURCE {resource_ids}. 해당 task revision에만 적용한다."""

    consent_id: str
    task_id: str
    task_revision: int = Field(ge=1)
    owner_actor_id: str
    axis: Axis
    scope: dict[str, Any]
    source_ref: str

    def covers(self, value: int | str | None) -> bool:
        if self.axis == "TIME":
            return (
                isinstance(value, int)
                and self.scope["start_min"] <= value <= self.scope["start_max"]
            )
        return value in self.scope["resource_ids"]


class ConsultationItem(Frozen):
    """기준 대비 바뀐 작업 1개 (§9.3). base_status만 저장하고 WAIVED 등은 조회 시 붙인다."""

    task_id: str
    task_revision: int = Field(ge=1)
    owner_actor_id: str
    before: Assignment
    after: Assignment
    change_hash: str
    base_status: Literal["COVERED", "PENDING"]


class Conflict(Frozen):
    """충돌 탐지 결과 (§6, 부록 A.10). interval은 관련 작업 점유를 모두 덮는 [start, end)."""

    rule_id: str
    task_ids: tuple[str, ...]  # 정렬
    resource_id: str | None = None
    zone_ids: tuple[str, ...]
    interval: tuple[int, int]


class PlanRef(Frozen):
    plan_revision: int = Field(ge=0)
    assignments: tuple[Assignment, ...]


class SnapshotContent(Frozen):
    """Snapshot.content의 구조 (부록 A.10). tasks는 현재 revision 중 READY만."""

    site_id: str
    pack_hash: str
    horizon_minutes: int = Field(gt=0)
    context_version: int = Field(ge=0)
    plan_revision: int = Field(ge=0)
    tasks: tuple[Task, ...]
    resources: tuple[Resource, ...]
    zones: tuple[str, ...]
    zone_relations: tuple[ZoneRelation, ...]
    plan: PlanRef
    holds: tuple[HoldRef, ...] = ()
    constraints: tuple[FeedbackConstraint, ...] = ()
    consents: tuple[Consent, ...] = ()

    def task_map(self) -> dict[str, Task]:
        return {t.task_id: t for t in self.tasks}

    def resource_map(self) -> dict[str, Resource]:
        return {r.resource_id: r for r in self.resources}

    def rel(self, zone_a: str, zone_b: str) -> Relation | None:
        """저장된 방향 그대로. 같은 zone이면 SAME, 선언이 없으면 None (§5.3)."""
        if zone_a == zone_b:
            return "SAME"
        for r in self.zone_relations:
            if r.zone_a == zone_a and r.zone_b == zone_b:
                return r.relation
        return None

    def base_assignments(self) -> dict[str, Assignment]:
        """READY 작업의 기준 배정. Plan에 있으면 Plan 값, 없으면 (earliest_start, 요청 자원)."""
        in_plan = {a.task_id: a for a in self.plan.assignments}
        return {
            t.task_id: in_plan.get(t.task_id)
            or Assignment(
                task_id=t.task_id,
                start=t.earliest_start,
                end=t.earliest_start + t.duration,
                resource_id=t.requested_resource_id,
            )
            for t in sorted(self.tasks, key=lambda t: t.task_id)
        }

    def check_assignments(self) -> tuple[Assignment, ...]:
        """검사 대상 배정 = 현재 Plan 배정 + Plan에 없는 READY 작업의 기준 배정."""
        in_plan = {a.task_id for a in self.plan.assignments}
        extra = [a for tid, a in self.base_assignments().items() if tid not in in_plan]
        return tuple(sorted((*self.plan.assignments, *extra), key=lambda a: a.task_id))


class Snapshot(Frozen):
    snapshot_id: str
    snapshot_hash: str
    content: dict[str, Any]

    def facts(self) -> SnapshotContent:
        return SnapshotContent.model_validate(self.content)


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

    # 표시용 판정은 저장하지 않고 status에서 계산한다 (부록 A.11)
    @property
    def solution(self) -> list[dict[str, Any]] | None:
        if self.chosen_stage == 1:
            return self.stage1.get("solution")
        if self.chosen_stage == 2 and self.stage2 is not None:
            return self.stage2.get("solution")
        return None

    @property
    def minimal_change(self) -> bool:
        return self.stage1.get("status") == "OPTIMAL"

    @property
    def delay_optimality_unconfirmed(self) -> bool:
        return self.solution is not None and (
            self.stage2 is None or self.stage2.get("status") != "OPTIMAL"
        )


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


# ── Agent 실행 상태 (§5.1·§11, 부록 A.16) ──────────────────────

RunStatus = Literal[
    "RUNNING",
    "WAITING_HUMAN",
    "SUCCEEDED",
    "ESCALATED",
    "BUDGET_EXHAUSTED",
    "STALE",
    "CANCELLED",
    "ERROR",
]
AgentType = Literal["REPLANNING", "COORDINATION", "INTAKE", "EVENT_RESPONSE", "ASSISTANT"]


class AgentRun(Frozen):
    run_id: str
    agent_type: AgentType
    case_id: str
    acting_actor_id: str | None
    acting_unit_id: str
    input_ref: dict[str, Any]
    exec_contract_version: str
    status: RunStatus
    wait_kind: Literal["MESSAGE", "CONSULTATION", "CANDIDATE_OUTCOME"] | None = None
    wait_ref: str | None = None
    wait_generation: int = Field(default=0, ge=0)
    wake_seq: int = Field(default=0, ge=0)
    handled_wake_seq: int = Field(default=0, ge=0)
    last_step_no: int = Field(default=0, ge=0)
    end_reason: str | None = None
    steps_used: int = Field(default=0, ge=0)
    llm_attempts_used: int = Field(default=0, ge=0)
    human_rounds_used: int = Field(default=0, ge=0)
    solver_calls_used: int = Field(default=0, ge=0)
    solver_seconds_used: float = Field(default=0, ge=0)
    restart_count: int = Field(default=0, ge=0)

    @property
    def budget_used(self) -> dict[str, float]:
        return {
            "steps": self.steps_used,
            "llm_attempts": self.llm_attempts_used,
            "human_rounds": self.human_rounds_used,
            "solver_calls": self.solver_calls_used,
            "solver_seconds": self.solver_seconds_used,
        }
