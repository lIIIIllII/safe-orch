// API 응답 타입. 서버가 dict를 돌려주므로 직접 쓴다.

export type Role = 'UNIT_PLANNER' | 'REPORTER' | 'SUPERVISOR'

export interface Site {
  site_id: string
  pack_hash: string
  horizon_start_utc: string
  horizon_minutes: number
  context_version: number
  plan_revision: number
  pack: string
}

export interface Actor {
  actor_id: string
  name: string
  unit_id: string
  roles: Role[]
}

export interface Unit {
  unit_id: string
  name: string
  unit_type: string
}

/** Pack이 선언한 자원 속성. 화면은 속성 이름을 모르고 선언으로만 그린다. */
export interface ResourceAttribute {
  name: string
  type: 'NUMBER' | 'LIST'
  unit: string
  display_name: string
}

/** 자원 요구 조건: GTE 수치 이상, LTE 수치 이하, CONTAINS 목록 포함. */
export interface Requirement {
  attribute: string
  op: 'GTE' | 'LTE' | 'CONTAINS'
  value: number | string
}

/** Pack이 선언한 수량 풀 종류 (직종, 같은 장비 여러 대 등). */
export interface PoolKind {
  kind: string
  display_name: string
  unit: string
}

/** 수량 풀: 여러 작업이 수량을 나눠 쓴다. 고르는 자원(Resource)과 다른 개념이다. */
export interface Pool {
  pool_id: string
  kind: string
  display_name: string
  owner_unit_id: string
  allowed_unit_ids: string[]
  quantity: number
  /** 데이터만 (meta.currency 단위) */
  cost_per_hour: number | null
}

/** 작업이 풀에서 쓰는 수량. required는 작업 유형 기본 수요에만 있다(필수 직종). */
export interface Demand {
  kind: string
  quantity: number
  required?: boolean
}

export interface Resource {
  resource_id: string
  display_name: string
  resource_type: string
  owner_unit_id: string
  allowed_unit_ids: string[]
  /** 사용 가능 구역. ["*"]는 모든 구역 */
  allowed_zone_ids: string[]
  available_intervals: [number, number][]
  /** 선언된 속성의 값 (수치 또는 문자열 목록) */
  attributes: Record<string, number | string[]>
  /** 데이터만 (meta.currency 단위) */
  cost_per_hour: number | null
  note: string
}

export interface Task {
  task_id: string
  revision: number
  unit_id: string
  owner_actor_id: string
  work_type: string
  hazard_tags: string[]
  zone_id: string
  duration: number
  earliest_start: number
  latest_start: number
  latest_end: number
  required_resource_type: string | null
  requested_resource_id: string | null
  /** 작업 유형 기본 요구 조건 (서버 도출) */
  default_requirements: Requirement[]
  /** 작업 값으로 더한 요구 조건 */
  resource_requirements: Requirement[]
  /** 작업 유형 기본 수요 (서버 도출) */
  default_demands: Demand[]
  /** 작업 값 수요. 기본 수요보다 큰 것만 반영된다 */
  pool_demands: Demand[]
  /** 자원 축이 담당자 확인으로 열렸는가. 시각이 움직이는지는 고정 여부로 정한다 */
  movable: { resource: boolean }
  /** 사람이 건 고정(누가·언제). 없으면 고정되지 않았다 */
  pin: { pin_id: string; pinned_by: string; by_role: 'OWNER' | 'SUPERVISOR'; pinned_at: string } | null
  /** 담당자가 그린 희망 영역 [start, end) (분). 서버는 강제하지 않는다 */
  preferred_window: { start: number; end: number; set_by: string; set_at: string } | null
  lifecycle: string
  gate: 'ALLOW' | 'HOLD' | 'STALE'
  reasons: string[]
}

export interface Assignment {
  task_id: string
  start: number
  end: number
  resource_id: string | null
}

export interface Plan {
  plan_revision: number
  assignments: Assignment[]
  candidate_id: string | null
  committed_context_version: number
}

export interface Conflict {
  rule_id: string
  task_ids: string[]
  resource_id: string | null
  zone_ids: string[]
  interval: [number, number]
  /** 풀 초과 충돌에만: 어느 풀·종류가 언제(at, 분) 수요 합이 수량을 넘었는가 */
  pool?: { pool_id: string; kind: string; at: number; demand: number; quantity: number }
}

export interface SolverView {
  scope_level: string
  /** 목적 순서. DELAY_FIRST면 1단계가 총 지연, 2단계가 변경 작업 수다 */
  objective: 'CHANGE_FIRST' | 'DELAY_FIRST'
  stage1: { status: string; changed: number | null; delay?: number | null }
  stage2: { status: string; delay: number | null; changed?: number | null; work_delay: number | null } | null
  chosen_stage: number | null
  minimal_change: boolean
  minimal_delay: boolean
  delay_optimality_unconfirmed: boolean
}

export interface ValidationCheck {
  check_id: string
  status: string
  task_ids: string[]
  reason_code: string | null
}

export interface ConsultationItem {
  task_id: string
  task_revision: number
  owner_actor_id: string
  before: Assignment
  after: Assignment
  change_hash: string
  base_status: string
  item_status: string
  /** 마지막 변경 요청과 담당자 답 (Coordination). quoted_comment는 담당자가 쓴 인용이다. */
  request: {
    message_id: string
    to_actor_id: string
    status: string
    decision: string | null
    quoted_comment: string | null
  } | null
  /** 상태를 만든 담당자 답의 출처. prior면 같은 변경에 다른 후보에서 한 답이 적용된 것이다. */
  answer_source: {
    message_id: string
    candidate_id: string
    actor_id: string | null
    at: string | null
    prior: boolean
  } | null
}

/** 거절된 후보의 거절 사유. */
export interface RejectionView {
  decision_id: string
  actor_id: string
  reason_code: string
  target_task_ids: string[]
  comment: string
  context_version: number
}

export interface CandidateView {
  candidate_id: string
  kind: 'REPLAN' | 'RECONFIRM'
  rejection: RejectionView | null
  run_id: string | null
  /** 이 후보를 만든 Case. 같은 Case의 안끼리 나란히 본다 */
  case_id: string | null
  /** 이 후보에 도달한 접근. no는 그 Case의 재계획 호출 순번("n안"). 여럿이면 다른 접근이 같은 배치를 냈다.
   *  quoted_note(메인)·quoted_reason(재계획)은 모델이 쓴 문장이다 */
  approaches: {
    no: number
    approach: string
    quoted_note: string | null
    run_id: string
    same: boolean
    quoted_reason: string | null
  }[]
  /** Supervisor가 고른 안. 고른 안만 협의한다 */
  chosen: boolean
  context_version: number
  base_plan_revision: number
  display_status: 'COMMITTED' | 'REJECTED' | 'STALE' | 'OPEN'
  assignments: Assignment[]
  /** delay = 달력 분, work_delay = 근무 분. 서버가 조회 시 계산한다. */
  changes: {
    task_id: string
    before: Assignment
    after: Assignment
    delay: number
    work_delay: number
  }[]
  solver: SolverView | null
  /** Agent가 그 탐색에 건 조건(서버가 받은 값, 시각은 분). 없으면 빈 목록 */
  conditions: {
    task_id: string
    start_min: number | null
    start_max: number | null
    resource_id: string | null
    /** 시작 범위가 담당자의 희망 영역에서 왔다 */
    preferred: boolean
  }[]
  /** 이 후보가 담은 변경 가운데 거절·이견된 것(서버 계산). REJECTION 거절된 후보의 대상 작업 변경과 같음, OBJECTION 담당자 이견 */
  contested: { task_id: string; by: 'REJECTION' | 'OBJECTION' }[]
  validation: {
    validation_id: string
    status: string
    display_status: string
    checks: ValidationCheck[]
  } | null
  consultation: { status: string; items: ConsultationItem[] } | null
}

export interface HoldView {
  hold_id: string
  scope: 'TASK' | 'SITE'
  task_id: string | null
  created_context_version: number
  event_id: string
  event_type: string
  text: string
  reporter_actor_id: string
  target_task_id: string | null
  /** 이 Event의 사실 수정안 */
  fact_updates: {
    proposal_id: string
    task_id: string
    field: string
    old_value: number
    new_value: number
    status: string
  }[]
}

export interface EventView {
  event_id: string
  source_event_id: string
  event_type: string
  text: string
  reporter_actor_id: string
  target_task_id: string | null
  context_version: number
  hold_id: string
  hold_status: string
}

export interface RunSummary {
  run_id: string
  agent_type: string
  case_id: string
  /** 부른 메인 Run. 메인과 Intake는 null */
  parent_run_id: string | null
  acting_unit_id: string
  status: string
  wait_kind: string | null
  wait_ref: string | null
  wait_generation: number
  /** 대기 뒤 다시 실행된 횟수 (서버가 조회 시 계산) */
  resume_count: number
  last_step_no: number
  current_step_status: string | null
  end_reason: string | null
  budget_used: Record<string, number>
  /** 이 Run의 한도. 메인만 온다(사람이 만든 일만큼 늘어난다) */
  budget_max: Record<string, number> | null
}

export interface SiteState {
  server_time: string
  site: Site
  actors: Actor[]
  units: Unit[]
  zones: string[]
  resources: Resource[]
  tasks: Task[]
  plan: Plan
  conflicts: Conflict[]
  candidates: CandidateView[]
  review_queue: string[]
  /** 대기열(QUEUED) 접수 순서 */
  task_queue: string[]
  holds: HoldView[]
  events: EventView[]
  runs: RunSummary[]
  dispatch: { pending: number; failed: number }
  /** X-Actor 본인에게 온 질문 */
  inbox: InboxItem[]
}

/** 받은 요청 항목. body = 서버 문구(동의 내용의 기준), agent_text = 모델 작성 설명. */
export interface InboxItem {
  message_id: string
  run_id: string
  step_no: number
  to_actor_id: string
  type: string
  status: 'OPEN' | 'ANSWERED' | 'CANCELLED' | 'LATE'
  body: string
  agent_text: string | null
  reply: { decision: string; values: string[]; comment: string; actor_id: string; at: string } | null
  created_context_version: number
  answered_context_version: number | null
  proposal_id: string | null
  proposal_type: string | null
  proposal_status: string | null
  task_id: string | null
  axis: string | null
  allowed_values: string[]
  /** 변경 요청이 묶인 후보 (CHANGE_REQUEST) */
  candidate_id: string | null
  /** 사실 수정안 (CONFIRMATION + FACT_UPDATE). 값은 Horizon 원점 기준 분 */
  fact: { field: string; old_value: number; new_value: number } | null
  /** 작업 요청 값 확인 (제안 없는 CONFIRMATION, Work Intake). 시간은 분 */
  values: IntakeValues | null
}

export interface IntakeValues {
  work_type: string
  zone_id: string
  duration: number
  earliest_start: number
  latest_start: number
  latest_end: number
  required_resource_type: string | null
  requested_resource_id: string | null
}

export interface AgentStep {
  run_id: string
  step_no: number
  status: 'RESERVED' | 'COMPLETED' | 'ABORTED'
  observed_context_version: number
  observed_plan_revision: number
  goal: string | null
  observation: unknown
  available_actions: unknown
  action: { name: string; args?: Record<string, unknown>; raw?: unknown } | null
  decision_summary: string | null
  tool_result: Record<string, unknown> | null
  guard: { verdict: string; reason_code: string | null } | null
  state_changes: unknown
  result_kind: string | null
  budget_remaining: Record<string, number> | null
  model_id: string | null
  prompt_version: string | null
  llm_attempts: number | null
  abort_reason: string | null
  created_at: string | null
}

/** 명령 응답. HTTP 코드와 네트워크 실패를 함께 담는다. */
export interface CommandResponse {
  status: 'APPLIED' | 'REPLAYED' | 'REJECTED' | 'RETRYABLE_ERROR' | 'NETWORK_ERROR'
  reason_codes: string[]
  context_version: number | null
  plan_revision: number | null
  result_refs: Record<string, unknown>
  detail?: unknown
  http: number | null
}

export interface CommandOutcome {
  label: string
  actor_id: string
  response: CommandResponse
}

/** GET /api/sites. X-Actor 없이 읽는 입구. */
export interface SiteEntry {
  site_id: string
  pack: string
  actors: { actor_id: string; name: string; roles: Role[] }[]
}

/** GET /api/sites/{id}/meta. 화면은 Pack 값을 여기서만 받는다. */
export interface Meta {
  pack: string
  pack_hash: string
  /** 자원 유형 코드 → 표시 이름 */
  resource_types: Record<string, string>
  resource_attributes: ResourceAttribute[]
  currency: string
  work_types: Record<
    string,
    {
      display_name: string
      hazard_tags: string[]
      critical_fields: string[]
      /** 이 유형 작업에 서버가 붙이는 기본 자원 요구 조건 */
      resource_requirements: Requirement[]
      /** 이 유형 작업에 서버가 붙이는 기본 수요와 필수 직종 */
      pool_demands: Demand[]
    }
  >
  pool_kinds: PoolKind[]
  pools: Pool[]
  rules: { rule_id: string; type: string; display_name: string }[]
  timezone: string
  horizon_start_utc: string
  horizon_minutes: number
  work_intervals: [number, number][]
  zones: string[]
  zone_relations: { zone_a: string; zone_b: string; relation: string }[]
  resources: Resource[]
}

/** 작업 요청 폼 본문 (시각은 원점 기준 분). */
export interface TaskForm {
  task_id: string
  work_type: string
  zone_id: string
  duration: number
  earliest_start: number
  latest_start: number
  latest_end: number
  required_resource_type: string | null
  requested_resource_id: string | null
  resource_requirements: Requirement[]
  pool_demands: Demand[]
}

/** GET /api/dev/scenario (DEMO_MODE). */
export interface Scenario {
  pack: string
  task_requests: { label: string; requester: string; form: TaskForm }[]
  event_reports: {
    label: string
    body: { event_type: 'DELAY' | 'OTHER'; text: string; target_task_id: string | null }
  }[]
  rejections: {
    label: string
    body: { reason_code: string; target_task_ids: string[]; comment: string }
  }[]
  /** 자연어 작업 요청 시연값 */
  intake_requests?: { label: string; requester: string; body: { task_id: string; text: string }; answer: string }[]
}
