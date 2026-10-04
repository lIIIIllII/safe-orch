"""Domain 엔티티.

app 내부 모듈을 import하지 않는다. 시간은 Horizon 원점 기준 정수 분, 점유는 [start, end).
조회 결과는 매번 새 객체로 만든다. 모델은 frozen이다.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

Role = Literal["UNIT_PLANNER", "REPORTER", "SUPERVISOR"]
Relation = Literal["SAME", "ADJACENT", "BELOW"]
StoredRelation = Literal["ADJACENT", "BELOW"]
Lifecycle = Literal["DRAFT", "NEEDS_INFO", "READY", "QUEUED"]  # QUEUED: 열린 Case 중 접수
FieldStatus = Literal["PROPOSED", "CONFIRMED"]
# MOVE: 담당자가 타임라인에서 자기 작업을 직접 옮긴 것 (AG-31)
CandidateKind = Literal["REPLAN", "RECONFIRM", "MOVE"]
ValidationStatus = Literal["PASS", "FAIL", "INCOMPLETE"]
ScopeLevel = Literal["L0", "L1", "L2"]
Axis = Literal["TIME", "RESOURCE"]
# 목적 순서: 변경 작업 수를 먼저 줄일지, 총 지연을 먼저 줄일지 (CV-27)
Objective = Literal["CHANGE_FIRST", "DELAY_FIRST"]
AttributeType = Literal["NUMBER", "LIST"]
RequirementOp = Literal["GTE", "LTE", "CONTAINS"]  # 코어가 아는 비교는 이 셋뿐이다 (CV-17)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ── Pack 정의 ──────────────────────────────────────────────────


class ResourceAttribute(Frozen):
    """Pack이 선언한 자원 속성. 코어는 속성 이름을 모른다 (CV-17)."""

    name: str
    type: AttributeType
    unit: str = ""
    display_name: str = Field(min_length=1)  # 화면 표시용


class Requirement(Frozen):
    """자원 요구 조건 하나. GTE·LTE는 NUMBER 속성과 수치, CONTAINS는 LIST 속성과 문자열."""

    attribute: str
    op: RequirementOp
    value: int | float | str


class PoolKind(Frozen):
    """Pack이 선언한 수량 풀 종류(직종, 같은 장비 여러 대 등). 코어는 종류 이름을 모른다."""

    kind: str
    display_name: str = Field(min_length=1)  # 화면 표시용
    unit: str = ""


class Demand(Frozen):
    """작업이 풀에서 쓰는 수량 (종류별)."""

    kind: str
    quantity: int = Field(gt=0)


class PoolDemand(Demand):
    """작업 유형의 기본 수요. required면 그 Unit에 이 종류의 풀이 있어야 한다(필수 직종)."""

    required: bool = False


class WorkType(Frozen):
    work_type: str
    display_name: str = Field(min_length=1)  # 화면 표시용
    hazard_tags: tuple[str, ...] = Field(min_length=1)
    critical_fields: tuple[str, ...]
    resource_requirements: tuple[Requirement, ...] = ()  # 이 유형 작업의 기본 요구 조건
    pool_demands: tuple[PoolDemand, ...] = ()  # 이 유형 작업의 기본 수요와 필수 직종


class Rule(Frozen):
    rule_id: str
    type: Literal["SEPARATION", "CAPACITY"]
    display_name: str = Field(min_length=1)  # 화면 표시용
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
    display_name: str = ""  # 화면 표시용
    resource_type: str
    owner_unit_id: str
    allowed_unit_ids: tuple[str, ...]
    allowed_zone_ids: tuple[str, ...]  # 필수. ("*",)는 모든 구역 (CV-18)
    capacity: Literal[1] = 1
    available_intervals: tuple[tuple[int, int], ...]
    attributes: dict[str, int | float | tuple[str, ...]] = Field(
        default_factory=dict
    )  # Pack 선언 속성 값
    cost_per_hour: int | float | None = None  # 데이터만. 계산·판정에 쓰지 않는다 (CV-22)
    note: str = ""


class Pool(Frozen):
    """수량 풀: 고르는 자원(수량 1)과 달리 여러 작업이 수량을 나눠 쓴다 (CV-23)."""

    pool_id: str
    kind: str
    display_name: str = ""  # 화면 표시용
    owner_unit_id: str
    allowed_unit_ids: tuple[str, ...]  # Pack에서 생략하면 로더가 소유 Unit만 넣는다
    quantity: int = Field(gt=0)
    cost_per_hour: int | float | None = None  # 단가. 데이터만 (CV-22)


def pool_for(pools: "tuple[Pool, ...] | list[Pool]", unit_id: str, kind: str) -> Pool | None:
    """그 Unit이 그 종류에 쓰는 풀. 로더가 하나임을 보장한다 (CV-23). 없으면 None."""
    for pool in sorted(pools, key=lambda p: p.pool_id):
        if pool.kind == kind and unit_id in pool.allowed_unit_ids:
            return pool
    return None


class Predecessor(Frozen):
    task_id: str
    min_lag: int = 0


class Movable(Frozen):
    """탐색 축(SearchSpec.axes): 그 작업의 시각·자원을 Solver가 바꿀 수 있는가."""

    time: bool
    resource: bool


class TaskMovable(Frozen):
    """작업의 자원 축이 담당자 확인으로 열렸는가. 시각이 움직이는지는 고정 여부로 정한다 (AG-27)."""

    resource: bool


class FieldRecord(Frozen):
    """critical field 하나의 확인 기록."""

    value: Any
    status: FieldStatus
    source_ref: str


class Task(Frozen):
    task_id: str
    revision: int = Field(ge=1)
    unit_id: str
    owner_actor_id: str
    work_type: str
    hazard_tags: tuple[str, ...]  # 서버가 Pack에서 도출
    zone_id: str
    duration: int = Field(gt=0)
    earliest_start: int
    latest_start: int
    latest_end: int
    required_resource_type: str | None = None
    requested_resource_id: str | None = None
    default_requirements: tuple[Requirement, ...] = ()  # 서버가 Pack의 작업 유형에서 도출 (CV-11)
    resource_requirements: tuple[Requirement, ...] = ()  # 작업 값. 기본값에 더하기만 한다 (CV-11)
    default_demands: tuple[PoolDemand, ...] = ()  # 서버가 Pack의 작업 유형에서 도출 (CV-11)
    pool_demands: tuple[Demand, ...] = ()  # 작업 값. 기본 수요보다 낮출 수 없다 (CV-11)
    predecessors: tuple[Predecessor, ...] = ()
    movable: TaskMovable
    fields: dict[str, FieldRecord]
    lifecycle: Lifecycle

    @property
    def requirements(self) -> tuple[Requirement, ...]:
        """자원이 모두 맞춰야 하는 조건 = 작업 유형 기본값 + 작업 값."""
        return (*self.default_requirements, *self.resource_requirements)

    @property
    def demands(self) -> dict[str, int]:
        """종류별 수요 = 작업 유형 기본값과 작업 값 중 큰 쪽. 작업 값으로 낮출 수 없다."""
        out: dict[str, int] = {}
        for d in (*self.default_demands, *self.pool_demands):
            out[d.kind] = max(out.get(d.kind, 0), d.quantity)
        return dict(sorted(out.items()))

    @property
    def required_kinds(self) -> tuple[str, ...]:
        """풀이 꼭 있어야 하는 종류(필수 직종)."""
        return tuple(sorted({d.kind for d in self.default_demands if d.required}))

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


class Pin(Frozen):
    """사람이 건 작업 고정(시각·자원 전체). task revision에 묶지 않는다 (AG-27)."""

    pin_id: str
    task_id: str
    pinned_by: str
    by_role: Literal["OWNER", "SUPERVISOR"]


class HoldRef(Frozen):
    """Snapshot에 넣는 ACTIVE Hold."""

    hold_id: str
    scope: Literal["TASK", "SITE"]
    task_id: str | None = None


class Consent(Frozen):
    """작업 담당자의 이동 동의. scope는 TIME {start_min, start_max},
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
    """기준 대비 바뀐 작업 1개. base_status만 저장하고 WAIVED 등은 조회 시 붙인다."""

    task_id: str
    task_revision: int = Field(ge=1)
    owner_actor_id: str
    before: Assignment
    after: Assignment
    change_hash: str
    base_status: Literal["COVERED", "PENDING"]


class PoolExcess(Frozen):
    """풀 초과 내용: 어느 풀·종류가 언제(at, 분) 수요 합이 수량을 넘었는가."""

    pool_id: str
    kind: str
    at: int
    demand: int
    quantity: int


class Conflict(Frozen):
    """충돌 탐지 결과. interval은 관련 작업 점유를 모두 덮는 [start, end)."""

    rule_id: str
    task_ids: tuple[str, ...]  # 정렬
    resource_id: str | None = None
    zone_ids: tuple[str, ...]
    interval: tuple[int, int]
    pool: PoolExcess | None = None  # 풀 초과 충돌에만 있다

    @model_serializer(mode="wrap")
    def _omit_empty_pool(self, handler: Any) -> dict[str, Any]:
        """pool은 풀 초과 충돌에서만 내보낸다(다른 충돌의 기록 모양은 그대로)."""
        data = handler(self)
        if data.get("pool") is None:
            data.pop("pool", None)
        return data


class PlanRef(Frozen):
    plan_revision: int = Field(ge=0)
    assignments: tuple[Assignment, ...]


class SnapshotContent(Frozen):
    """Snapshot.content의 구조. tasks는 현재 revision 중 READY만."""

    site_id: str
    pack_hash: str
    horizon_minutes: int = Field(gt=0)
    work_intervals: tuple[tuple[int, int], ...] = Field(min_length=1)  # 근무 달력
    context_version: int = Field(ge=0)
    plan_revision: int = Field(ge=0)
    tasks: tuple[Task, ...]
    resources: tuple[Resource, ...]
    pools: tuple[Pool, ...] = ()
    zones: tuple[str, ...]
    zone_relations: tuple[ZoneRelation, ...]
    plan: PlanRef
    holds: tuple[HoldRef, ...] = ()
    pins: tuple[Pin, ...] = ()
    consents: tuple[Consent, ...] = ()

    def task_map(self) -> dict[str, Task]:
        return {t.task_id: t for t in self.tasks}

    def pinned_task_ids(self) -> set[str]:
        return {p.task_id for p in self.pins}

    def resource_map(self) -> dict[str, Resource]:
        return {r.resource_id: r for r in self.resources}

    def rel(self, zone_a: str, zone_b: str) -> Relation | None:
        """저장된 방향 그대로. 같은 zone이면 SAME, 선언이 없으면 None."""
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


class Condition(Frozen):
    """Agent가 한 작업에 건 탐색 조건 (CV-24). 좁히기만 한다: 시작 범위 [start_min, start_max](분),
    자원 지정. preferred는 시작 범위가 희망 영역에서 왔다는 표시다."""

    start_min: int | None = None
    start_max: int | None = None
    resource_id: str | None = None
    preferred: bool = False


class SearchSpec(Frozen):
    search_spec_id: str
    hash: str
    snapshot_id: str
    acting_unit_id: str
    scope_level: ScopeLevel
    axes: dict[str, Movable]
    resource_alternatives: dict[str, tuple[str, ...]]
    # Agent가 건 조건(작업별). 없으면 빈 dict (CV-24)
    conditions: dict[str, Condition] = Field(default_factory=dict)
    objective: Objective = "CHANGE_FIRST"
    time_limit_s: int = Field(gt=0)
    # 실효 탐색 키(미시도 판정용, Solver 입력만). 무결성 hash와 다르다
    search_key: str


class SolverResult(Frozen):
    solver_result_id: str
    search_spec_id: str
    stage1: dict[str, Any]
    stage2: dict[str, Any] | None
    chosen_stage: Literal[1, 2] | None

    # 표시용 판정은 저장하지 않고 status에서 계산한다
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
    moved_by: str | None = None  # MOVE 후보를 만든 사람


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


# ── Agent 실행 상태 ──────────────────────

RunStatus = Literal[
    "RUNNING",
    "WAITING_HUMAN",
    "SUCCEEDED",
    "ESCALATED",
    "BLOCKED",
    "BUDGET_EXHAUSTED",
    "STALE",
    "CANCELLED",
    "ERROR",
]
AgentType = Literal["REPLANNING", "COORDINATION", "INTAKE", "EVENT_RESPONSE", "ASSISTANT", "MAIN"]
WaitKind = Literal["MESSAGE", "CONSULTATION", "CANDIDATE_OUTCOME", "CHILD_RUN", "HUMAN_DECISION"]


class AgentRun(Frozen):
    run_id: str
    agent_type: AgentType
    case_id: str
    parent_run_id: str | None = None
    acting_actor_id: str | None
    acting_unit_id: str
    input_ref: dict[str, Any]
    exec_contract_version: str
    status: RunStatus
    wait_kind: WaitKind | None = None
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
    agent_calls_used: int = Field(default=0, ge=0)
    last_event_seq: int = Field(default=0, ge=0)

    @property
    def budget_used(self) -> dict[str, float]:
        return {
            "steps": self.steps_used,
            "llm_attempts": self.llm_attempts_used,
            "human_rounds": self.human_rounds_used,
            "solver_calls": self.solver_calls_used,
            "solver_seconds": self.solver_seconds_used,
            "agent_calls": self.agent_calls_used,
        }
