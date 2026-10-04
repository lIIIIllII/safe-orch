// 코드 → 한국어 문구. 화면은 문구와 원래 코드를 함께 보여 주고, 표에 없는 코드는 코드만 보여 준다.
// Pack과 무관한 공통 코드(reason_code, 상태값, 기본 제약 id)만 둔다. 작업 유형·Pack Rule 이름은 meta에서 받는다.

export const REASON: Record<string, string> = {
  // 응답 status
  APPLIED: '적용됨',
  REPLAYED: '이미 처리된 요청(같은 결과)',
  REJECTED: '거절됨',
  RETRYABLE_ERROR: '잠시 후 재시도 필요(잠금 대기 초과)',
  NETWORK_ERROR: '서버 응답 없음',
  // 승인
  NOT_AUTHORIZED: '현재 Actor에게 권한 없음',
  CANDIDATE_NOT_FOUND: '후보 없음',
  CANDIDATE_REJECTED: '이미 거절된 후보',
  CANDIDATE_COMMITTED: '이미 확정된 후보',
  ALREADY_CHOSEN: '이미 고른 안',
  CANDIDATE_NOT_CHOSEN: 'Supervisor가 고르지 않은 안(협의 불가)',
  APPROACH_REQUIRED: '재계획 호출에 접근 필요',
  VALIDATION_NOT_PASS: '규칙 검사 통과 기록이 없는 후보',
  STALE_PLAN: '기준 계획이 이미 바뀜(다른 후보 확정)',
  STALE_CONTEXT: '후보를 만든 뒤 현장 정보가 바뀜',
  HOLD_ACTIVE: '해제되지 않은 Hold 있음',
  CONSULTATION_INCOMPLETE: '담당자 협의 미완료',
  // WAIVE
  CONSULTATION_NOT_FOUND: '협의 정보가 아직 없음',
  ITEM_NOT_FOUND: '협의 항목에 없는 작업',
  ITEM_NOT_WAIVABLE: '동의 대기 항목만 수용 가능',
  COMMENT_REQUIRED: '사유 필요(수용·이견)',
  // 거절
  INVALID_REASON_CODE: '거절 사유 코드 오류',
  TASK_NOT_FOUND: '대상 작업 없음',
  // 고정·희망 영역
  ALREADY_PINNED: '이미 고정된 작업',
  MOVE_NOT_VALID: '그 자리로 옮길 수 없음',
  EDIT_BREAKS_PLAN: '계획에 있는 작업의 지금 배치가 깨짐(먼저 옮기거나 없애기)',
  REMOVE_NOT_VALID: '이 작업을 없앨 수 없음',
  REMOVE_NOT_OWNER: '없앤 사람이 담당자가 아님',
  REMOVE_NOT_SINGLE_TASK: '없애기는 작업 하나만 뺌',
  NO_CHANGE: '바뀐 것이 없음',
  TASK_NOT_IN_PLAN: '계획에 없는 작업',
  MOVE_NOT_OWNER: '옮긴 사람이 담당자가 아님',
  MOVE_NOT_SINGLE_TASK: '직접 이동은 작업 하나만 바꿈',
  PIN_NOT_FOUND: '고정되지 않은 작업',
  PREFERRED_WINDOW_NOT_FOUND: '희망 영역이 없는 작업',
  // 요청 철회
  TASK_IN_PLAN: '확정 계획에 있는 작업은 철회 불가',
  TASK_HAS_SUCCESSORS: '이 작업을 선행으로 둔 요청이 있음(후속 요청을 먼저 철회)',
  // Event·Hold
  SOURCE_BODY_MISMATCH: '같은 신고 ID에 다른 내용',
  HOLD_NOT_FOUND: 'Hold 없음',
  HOLD_NOT_ACTIVE: '이미 해제된 Hold',
  RESOLUTION_NOT_SUPPORTED: '지원하지 않는 해제 사유',
  FACT_NOT_CONFIRMED: '이 신고의 사실 수정이 아직 확정되지 않음',
  // Work Intake
  INVALID_DECISION: '이 메시지에 쓸 수 없는 답(질문은 답변, 확인은 확인·거절)',
  TASK_ID_IN_INTAKE: '같은 작업 ID로 진행 중인 자연어 요청 있음',
  TASKSPEC_INVALID: '작업 요청 값이 검증을 통과하지 못함',
  NOTHING_TO_ASK: '모호·빠짐으로 적은 필드가 없어 물을 것이 없음',
  // 폼
  TASK_ID_EXISTS: '같은 작업 ID 있음',
  FIELD_MISSING: '필수 확인 항목 누락',
  UNKNOWN_WORK_TYPE: 'Pack에 없는 작업 유형',
  UNKNOWN_ZONE: '없는 구역',
  UNKNOWN_RESOURCE: '없는 자원',
  RESOURCE_TYPE_MISMATCH: '요청 자원 유형 불일치',
  RESOURCE_NOT_AUTHORIZED: '이 Unit이 쓸 수 없는 자원',
  RESOURCE_NO_AVAILABILITY: '가용 구간이 없는 자원',
  RESOURCE_ZONE_NOT_ALLOWED: '이 구역에서 쓸 수 없는 자원',
  RESOURCE_REQUIREMENT_NOT_MET: '자원이 요구 조건을 맞추지 못함',
  INVALID_REQUIREMENT: '요구 조건이 속성 선언과 맞지 않음',
  INVALID_DEMAND: '수요가 풀 종류 선언과 맞지 않음',
  REQUIRED_POOL_MISSING: '필수 직종의 풀이 이 Unit에 없음',
  INVALID_WINDOW: '시간창 모순',
  WINDOW_OUTSIDE_WORK_HOURS: '시간창 안에 근무시간 시작 자리가 없음',
  PREDECESSOR_NOT_FOUND: '선행 작업 없음',
  // 답변·확인
  MESSAGE_NOT_FOUND: '메시지 없음',
  PROPOSAL_NOT_FOUND: '제안 없음',
  ALREADY_ANSWERED: '이미 다른 결정으로 답함',
  STALE_PROPOSAL: '질문 뒤 작업이 바뀜(오래된 제안)',
  INVALID_VALUES: '허용 값 밖',
  // Run
  RUN_NOT_FOUND: 'Run 없음',
  RUN_NOT_ACTIVE: '이미 끝난 Run',
  // API 층
  ACTOR_REQUIRED: 'Actor 정보 없음',
  UNKNOWN_ACTOR: '등록되지 않은 Actor',
  IDEMPOTENCY_KEY_REQUIRED: '요청 키 없음',
  INVALID_IDEMPOTENCY_KEY: '요청 키 형식 오류',
  IDEMPOTENCY_MISMATCH: '같은 키에 다른 요청',
  INVALID_BODY: '입력 형식 오류',
  SITE_NOT_FOUND: '현장 없음',
  NOT_FOUND: '없음(DEMO_MODE가 아닐 수 있음)',
  CONFIRM_REQUIRED: '초기화 확인 문구 불일치',
  PACK_NOT_SUPPORTED: '지원하지 않는 Pack',
  WORKER_BUSY: '작업 처리 중이라 초기화 불가',
  // Gate reasons
  NOT_IN_PLAN: '확정 계획에 없음',
  CONTEXT_CHANGED: '확정 후 현장 정보 변경',
  // step guard
  ACTION_NOT_AVAILABLE: '허용 목록 밖 행동(차단)',
  STALE_OBSERVATION: '관찰 뒤 상태 변경, 재관찰',
  MALFORMED: '모델 응답 형식 오류',
  LLM_ERROR: '모델 호출 실패',
  LLM_CONFIG: '모델 설정 오류',
  STALE_SNAPSHOT: '계산 중 상태 변경, 결과 폐기',
  DUPLICATE_REJECTED: '거절된 배정과 같아 후보 만들지 않음',
  SAME_AS_EXISTING: '살아 있는 기존 후보와 같은 배치(새 후보 없음)',
  ALREADY_TRIED: '같은 범위·같은 조건은 이미 계산함',
  CONDITION_INVALID: '조건 모양 오류',
  CONDITION_OUTSIDE_WINDOW: '조건이 작업 시간창 밖',
  CONDITION_TASK_NOT_IN_SCOPE: '조건을 건 작업이 탐색 범위 밖',
  NO_PREFERRED_WINDOW: '희망 영역이 없는 작업',
  NEW_CHANGE_BEFORE_WAIT: '대기 직전 새 변화, 다시 관찰',
  RUN_INACTIVE: 'Run 종료됨',
  NO_ACTING_TASKS: '움직일 수 있는 작업 없음',
  RESOURCE_AXIS_NOT_ALLOWED: '자원 축 이동 불가',
  TIME_AXIS_NOT_ALLOWED: '시간 축 이동 불가',
  // 메인 도구의 거절 사유
  CHILD_RUN_OPEN: '부른 하위 Run이 아직 열려 있음',
  UNIT_NOT_IN_GROUP: '그 충돌 그룹에 작업이 없는 Unit',
  UNIT_HAS_NO_MOVABLE_TASK: '그 Unit에 움직일 수 있는 작업이 없음',
  // 사전 확인(담당자에게 대체 자원 허용을 묻기)의 거절 사유
  NEED_NOT_FOUND: '지금 결과에 없는 필요한 것',
  NOT_OWNER_CONSENT: '담당자 확인이 아닌 필요한 것',
  TIME_AXIS_NOT_ASKABLE: '시간 축은 사전 확인 대상이 아님',
  NO_VALUES: '물을 자원 값이 없음',
  VALUE_DECLINED: '담당자가 이미 거절한 값',
  ALREADY_CONSENTED: '이미 동의 범위에 있음',
  ALREADY_ASKED: '같은 작업에 답을 기다리는 질문이 있음',
  GROUP_NOT_FOUND: '지금 없는 충돌 그룹',
  SAME_FACTS: '마지막 결과 뒤로 관련 사실이 바뀌지 않음',
  NEW_EVENT: '아직 보지 않은 사건이 있음',
  OPEN_WORK: '이 Case에 열린 일이 남아 있음',
  NOTHING_TO_WAIT_FOR: '기다릴 후보·Hold가 없음',
  // end_reason
  CLOSE: '맡은 일을 마치고 종료',
  PARENT_ENDED: '부른 메인이 끝나 함께 종료',
  MALFORMED_TWICE: '응답 형식 오류 연속 2회로 종료',
  LLM_ERROR_TWICE: '모델 호출 실패 연속 2회로 종료',
  MODEL_VALIDATION_MISMATCH: 'Solver·검증 불일치(오류)',
  RECURSION_LIMIT: '반복 한도 초과(오류)',
  CANCELLED: '취소됨',
  ESCALATE: 'Supervisor에게 이관',
  RETURN_DONE: '결과를 돌려주고 종료',
  RETURN_BLOCKED: '막힌 결과를 돌려주고 종료',
  NEED_INVALID: '결과의 필요한 것이 가리키는 대상이 없음',
  RESTART: '재기동으로 중단',
  // Validator check 사유
  CANDIDATE_HASH_MISMATCH: '후보 hash 불일치',
  SNAPSHOT_REF_MISMATCH: 'Snapshot 참조 불일치',
  SNAPSHOT_HASH_MISMATCH: 'Snapshot hash 불일치',
  SEARCH_SPEC_MISSING: 'SearchSpec 없음',
  SEARCH_SPEC_UNEXPECTED: '재확정 후보에 SearchSpec 있음',
  SEARCH_SPEC_REF_MISMATCH: 'SearchSpec 참조 불일치',
  SEARCH_SPEC_HASH_MISMATCH: 'SearchSpec hash 불일치',
  PACK_HASH_MISMATCH: 'Pack hash 불일치',
  CONTEXT_VERSION_MISMATCH: 'Context 버전 불일치',
  PLAN_REVISION_MISMATCH: 'Plan revision 불일치',
  TASK_MISSING: '필수 작업 누락',
  TASK_DUPLICATE: '작업 중복',
  TASK_UNKNOWN: '없는 작업 포함',
  DURATION: 'duration 불일치',
  WINDOW: '시간창 위반',
  PRECEDENCE: '선후행 위반',
  PREDECESSOR_MISSING: '선행 작업이 계획 대상에 없음',
  OUTSIDE_ACTING_UNIT: '다른 Unit 작업 변경',
  TASK_PINNED: '고정된 작업 변경',
  RESOURCE_NOT_IN_SPEC: '허용 대안 밖 자원',
  RESOURCE_MISSING: '필요 자원 미배정',
  RESOURCE_TYPE: '자원 유형 부적격',
  RESOURCE_AUTH: '자원 사용 권한 없음',
  RESOURCE_ZONE: '자원 사용 가능 구역 밖',
  RESOURCE_REQUIREMENT: '자원이 요구 조건을 맞추지 못함',
  POOL_CAPACITY: '수량 풀 초과(겹치는 작업의 수요 합이 수량을 넘음)',
  POOL_MISSING: '필수 직종의 풀 없음',
  AVAILABILITY: '가용 구간 밖',
  CALENDAR: '근무시간 밖(근무 구간 하나에 들어가지 않음)',
  CAPACITY: '자원 겹침',
  FIELD_NOT_CONFIRMED: '필수 항목 미확인',
  CONFIRMED_VALUE_MISMATCH: '확인 값과 다름',
  HAZARD_TAGS_MISMATCH: '위험 태그가 Pack 도출값과 다름',
  DEFAULT_REQUIREMENTS_MISMATCH: '기본 자원 요구 조건이 Pack 도출값과 다름',
  DEFAULT_DEMANDS_MISMATCH: '기본 수요가 Pack 도출값과 다름',
  UNMAPPED_RULE: '처리할 수 없는 Rule',
}

export function reason(code: string): string | undefined {
  return REASON[code]
}

/** Gate reason: `HOLD:<id>`는 접두어로 푼다. */
export function gateReason(code: string): string {
  if (code.startsWith('HOLD:')) return `Hold 적용 중 (${code.slice(5)})`
  return REASON[code] ?? code
}

/** end_reason: `COMMITTED:1`, `EVENT:evt_…`처럼 접두어가 있는 값. */
export function endReason(code: string | null): string {
  if (!code) return ''
  const i = code.indexOf(':')
  if (i < 0) return REASON[code] ?? code
  const [head, rest] = [code.slice(0, i), code.slice(i + 1).trim()]
  switch (head) {
    case 'COMMITTED':
      return `R${rest} 확정으로 종료`
    case 'EVENT':
      return `지연 신고로 중단 (${rest})`
    case 'WITHDRAW':
      return `요청 ${rest} 철회로 중단`
    case 'CANCELLED_BY':
      return `${rest}이(가) 취소`
    case 'MODEL_UNAVAILABLE':
      return `모델 준비 실패: ${rest}`
    case 'TASKSPEC_COMPLETE':
      return `작업 요청 ${rest} 접수 완료`
    case 'FACT_CONFIRMED':
      return `사실 수정 확정으로 종료 (${rest})`
    case 'HOLD_RELEASED':
      return `변경 없음 해제로 중단 (${rest})`
    case 'CONSTRAINT':
      return `담당자가 제약을 확정해 후보 무효 (${rest})`
    case 'REJECTED':
      return `후보 거절로 중단 (${rest})`
    case 'AGENT_TYPE_NOT_REGISTERED':
      return `등록되지 않은 Agent 유형: ${rest}`
    case 'EXCEPTION':
      return `예외: ${rest}`
    case 'LLM_CONFIG':
      return `모델 설정 오류: ${rest}`
    default:
      return code
  }
}

export const CANDIDATE_STATUS: Record<string, string> = {
  OPEN: '검토 대기',
  STALE: 'STALE',
  REJECTED: '거절됨',
  COMMITTED: '확정됨',
}

export const CANDIDATE_KIND: Record<string, string> = {
  REPLAN: '재계획',
  RECONFIRM: '재확정',
  MOVE: '직접 이동',
  REMOVE: '작업 없애기',
}

/** Validation 대표 상태 배지. PASS 문구는 "정의된 규칙 검사 통과". */
export const VALIDATION_BADGE: Record<string, { icon: string; text: string; cls: string }> = {
  PASS: { icon: '✓', text: '정의된 규칙 검사 통과', cls: 'pass' },
  FAIL: { icon: '✕', text: '규칙 위반', cls: 'fail' },
  INCOMPLETE: { icon: '?', text: '입력 미확인', cls: 'incomplete' },
  STALE: { icon: '↻', text: '기준 변경됨(STALE)', cls: 'stale' },
}

export const CHECK_NAME: Record<string, string> = {
  C01: '무결성',
  C02: '작업 보존',
  C03: 'duration',
  C04: '시간창·근무시간',
  C05: '선후행',
  C06: '변경 범위',
  C07: '자원 적격',
  C08: '자원 권한',
  C09: '가용·용량',
  C10: '안전 분리',
  C11: '입력 완전성',
}

export const CHECK_STATUS: Record<string, string> = {
  PASS: '통과',
  FAIL: '위반',
  INCOMPLETE: '미확인',
}

export const SOLVER_STATUS: Record<string, string> = {
  OPTIMAL: '최적(이 탐색 범위 안)',
  FEASIBLE: '해 있음(최적 미확인)',
  INFEASIBLE: '이 탐색 범위에서 해 없음',
  UNKNOWN: '판정 못 함(불가능 아님)',
  MODEL_INVALID: '모델 오류',
}

export const SCOPE_LEVEL: Record<string, string> = {
  L0: 'L0 — 충돌 당사자만',
  L1: 'L1 — 같은 구역·같은 자원 작업까지',
  L2: 'L2 — Horizon 전체 작업',
}

export const CONSULTATION_STATUS: Record<string, string> = {
  COMPLETE: '협의 완료',
  OPEN: '협의 진행 중',
  BLOCKED: '이견으로 막힘',
  CANCELLED: '협의 취소(후보 STALE)',
}

export const ITEM_STATUS: Record<string, string> = {
  COVERED: '기존 동의 범위',
  PENDING: '담당자 동의 필요',
  WAIVED: 'Supervisor 수용',
  ACCEPTED: '담당자 수용',
  OBJECTED: '담당자 이견',
  CANCELLED: '협의 취소',
}

export const AGENT_TYPE: Record<string, string> = {
  MAIN: '메인 Agent',
  REPLANNING: '재계획 Agent',
  COORDINATION: '협의 Agent',
  INTAKE: '작업 접수 Agent',
  EVENT_RESPONSE: '신고 대응 Agent',
  ASSISTANT: '현장 도우미',
}

export const RUN_STATUS: Record<string, string> = {
  RUNNING: '실행 중',
  WAITING_HUMAN: '사람 대기',
  SUCCEEDED: '성공',
  ESCALATED: '이관',
  BLOCKED: '막힘',
  BUDGET_EXHAUSTED: 'Budget 소진',
  STALE: 'STALE',
  CANCELLED: '취소',
  ERROR: '오류',
}

export const WAIT_KIND: Record<string, string> = {
  CANDIDATE_OUTCOME: '후보 검증 결과 대기',
  CHILD_RUN: '부른 전문 Agent의 결과 대기',
  HUMAN_DECISION: '사람의 결정 대기(승인·거절, Hold 해제)',
  MESSAGE: '답변 대기',
  CONSULTATION: '협의 답변 대기',
}

export const STEP_STATUS: Record<string, string> = {
  RESERVED: '예약',
  COMPLETED: '완료',
  ABORTED: '중단',
}

export const RESULT_KIND: Record<string, string> = {
  CONTINUE: '계속',
  WAIT: '대기',
  DONE: '종료',
  REJECTED: '거절',
  INACTIVE: 'Run 종료됨',
}

export const ACTION_NAME: Record<string, string> = {
  CALL_AGENT: '전문 Agent 부르기',
  WAIT: '사람의 결정 기다리기',
  ESCALATE: 'Supervisor에게 이관',
  CLOSE: '끝내기',
  SOLVE_WITH_SCOPE: '탐색 범위 지정 Solver 실행',
  RETURN_RESULT: '결과 돌려주기',
  LIST_ASSIGNABLE_RESOURCES: '사용 가능 자원 조회',
  TRY_ALTERNATIVE_RESOURCE: '대체 자원 시도',
  ASK_TASK_OWNER: '작업 담당자에게 확인 요청',
  ASK_OWNER: '작업 담당자에게 사전 확인',
}

/** 메인이 재계획에 준 접근(무엇을 우선할지) */
export const APPROACH: Record<string, string> = {
  MIN_CHANGE: '변경 최소',
  MIN_DELAY: '지연 최소',
  PREFER_WINDOW: '희망 영역 우선',
}

export const OBJECTIVE: Record<string, string> = {
  CHANGE_FIRST: '변경 먼저',
  DELAY_FIRST: '지연 먼저',
}

/** 후보가 담은 거절·이견된 변경의 출처 (서버 계산) */
export const CONTESTED_BY: Record<string, string> = {
  REJECTION: '거절된 변경과 같음',
  OBJECTION: '담당자 이견',
}

export const ROLE: Record<string, string> = {
  UNIT_PLANNER: '공정 담당',
  REPORTER: '현장 신고',
  SUPERVISOR: '현장 감독',
}

export const REJECT_REASON: Record<string, string> = {
  RESOURCE_UNAVAILABLE: '자원 사용 불가',
  TIME_WINDOW_UNACCEPTABLE: '시간 수용 불가',
  PREFERENCE: '선호',
  OTHER: '기타',
}

export const GATE: Record<string, string> = {
  ALLOW: '시작 가능',
  HOLD: '보류',
  STALE: '재확정 필요',
}

export const MESSAGE_STATUS: Record<string, string> = {
  OPEN: '답변 대기',
  ANSWERED: '답함',
  CANCELLED: '취소됨(Run 종료)',
  LATE: '늦은 답(효력 없음)',
}

export const PROPOSAL_STATUS: Record<string, string> = {
  PENDING: '확인 대기',
  CONFIRMED: '확인됨',
  DISCARDED: '폐기됨',
  STALE: '효력 잃음',
}

export const DECISION: Record<string, string> = {
  ACCEPT: '수락',
  DECLINE: '거절',
  ANSWER: '답변',
}

/** 받은 요청 유형 */
export const MESSAGE_TYPE: Record<string, string> = {
  QUESTION: '담당자 확인 질문',
  CHANGE_REQUEST: '변경 요청',
  FACT_UPDATE: '사실 수정 확인',
  FREE_QUESTION: '확인 질문(답 입력)',
  NOTICE: '확정 통지',
}

/** 유형별 답 버튼·답 표시 문구. 변경 요청의 DECLINE은 이견, 사실 수정의 ACCEPT는 확정이다. */
export const DECISION_BY_TYPE: Record<string, Record<string, string>> = {
  CHANGE_REQUEST: { ACCEPT: '수락', DECLINE: '이견' },
  FACT_UPDATE: { ACCEPT: '확정', DECLINE: '폐기' },
}

/** 작업 값 이름 (Agent가 정한 값 표시, 카드에서 고치기) */
export const VALUE_NAME: Record<string, string> = {
  work_type: '작업 유형',
  zone_id: '구역',
  duration: '작업 시간',
  earliest_start: '가장 이른 시작',
  latest_start: '가장 늦은 시작',
  latest_end: '종료 한도',
  required_resource_type: '자원 유형',
  requested_resource_id: '요청 자원',
}

/** 사실 수정 필드 */
export const FACT_FIELD: Record<string, string> = {
  earliest_start: '시작 가능 시각',
}

export const AXIS: Record<string, string> = {
  TIME: '시간',
  RESOURCE: '자원',
}

/** 자원 조회 제외 사유 */
export const EXCLUDE_REASON: Record<string, string> = {
  NOT_ALLOWED: '이 Unit 사용 권한 없음',
  NO_AVAILABILITY: '가용 구간 없음',
  ZONE_NOT_ALLOWED: '작업 구역에서 쓸 수 없음',
  REQUIREMENT_NOT_MET: '요구 조건 미달',
  TYPE_MISMATCH: '자원 유형 다름',
}
