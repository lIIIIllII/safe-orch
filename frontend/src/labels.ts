// 코드 → 한국어 문구 (부록 A.19). 화면은 문구와 원래 코드를 함께 보여 주고, 표에 없는 코드는 코드만 보여 준다.

export const REASON: Record<string, string> = {
  // 응답 status
  APPLIED: '적용됨',
  REPLAYED: '이미 처리된 요청(같은 결과)',
  REJECTED: '거절됨',
  RETRYABLE_ERROR: '잠시 후 재시도 필요(잠금 대기 초과)',
  NETWORK_ERROR: '서버 응답 없음',
  // 승인 (A.14 검사 순서)
  NOT_AUTHORIZED: '현재 Actor에게 권한 없음',
  CANDIDATE_NOT_FOUND: '후보 없음',
  CANDIDATE_REJECTED: '이미 거절된 후보',
  VALIDATION_NOT_PASS: '규칙 검사 통과 기록이 없는 후보',
  STALE_PLAN: '기준 계획이 이미 바뀜(다른 후보 확정)',
  STALE_CONTEXT: '후보를 만든 뒤 현장 정보가 바뀜',
  HOLD_ACTIVE: '해제되지 않은 Hold 있음',
  CONSULTATION_INCOMPLETE: '담당자 협의 미완료',
  // WAIVE
  CONSULTATION_NOT_FOUND: '협의 정보가 아직 없음',
  ITEM_NOT_FOUND: '협의 항목에 없는 작업',
  ITEM_NOT_WAIVABLE: '동의 대기 항목만 수용 가능',
  COMMENT_REQUIRED: '수용 사유 필요',
  // 거절
  INVALID_REASON_CODE: '거절 사유 코드 오류',
  TARGET_REQUIRED: '작업 고정 거절은 대상 작업과 축 필요',
  TASK_NOT_FOUND: '대상 작업 없음',
  // Event·Hold
  SOURCE_BODY_MISMATCH: '같은 신고 ID에 다른 내용',
  HOLD_NOT_FOUND: 'Hold 없음',
  HOLD_NOT_ACTIVE: '이미 해제된 Hold',
  RESOLUTION_NOT_SUPPORTED: '사실 확정 해제는 아직 미지원(변경 없음만)',
  // 폼
  TASK_ID_EXISTS: '같은 작업 ID 있음',
  FIELD_MISSING: '필수 확인 항목 누락',
  UNKNOWN_WORK_TYPE: 'Pack에 없는 작업 유형',
  UNKNOWN_ZONE: '없는 구역',
  UNKNOWN_RESOURCE: '없는 자원',
  RESOURCE_TYPE_MISMATCH: '요청 자원 유형 불일치',
  RESOURCE_NOT_AUTHORIZED: '이 Unit이 쓸 수 없는 자원',
  INVALID_WINDOW: '시간창 모순',
  PREDECESSOR_NOT_FOUND: '선행 작업 없음',
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
  RUN_INACTIVE: 'Run 종료됨',
  NO_ACTING_TASKS: '움직일 수 있는 작업 없음',
  RESOURCE_AXIS_NOT_ALLOWED: '자원 축 이동 불가',
  TIME_AXIS_NOT_ALLOWED: '시간 축 이동 불가',
  // end_reason
  ESCALATE_NO_SOLUTION: '해를 찾지 못해 이관',
  MALFORMED_TWICE: '응답 형식 오류 연속 2회로 이관',
  LLM_ERROR_TWICE: '모델 호출 실패 연속 2회로 이관',
  MODEL_VALIDATION_MISMATCH: 'Solver·검증 불일치(오류)',
  RECURSION_LIMIT: '반복 한도 초과(오류)',
  CANCELLED: '취소됨',
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
  OUTSIDE_ACTING_UNIT: '다른 Unit 작업 변경',
  FROZEN_BY_CONSTRAINT: '확인된 제약으로 고정된 축 변경',
  RESOURCE_NOT_IN_SPEC: '허용 대안 밖 자원',
  RESOURCE_MISSING: '필요 자원 미배정',
  RESOURCE_TYPE: '자원 유형 부적격',
  RESOURCE_AUTH: '자원 사용 권한 없음',
  AVAILABILITY: '가용 구간 밖',
  CAPACITY: '자원 겹침',
  FIELD_NOT_CONFIRMED: '필수 항목 미확인',
  CONFIRMED_VALUE_MISMATCH: '확인 값과 다름',
  HAZARD_TAGS_MISMATCH: '위험 태그가 Pack 도출값과 다름',
  UNMAPPED_RULE: '처리할 수 없는 Rule',
  'SEP-LIFT-BELOW': '인양–하부 작업 분리',
  'SEP-HOT-FLAM': '화기–인화성 작업 분리(15분)',
  'CAP-RESOURCE': '자원 중복 배정 금지',
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
    case 'CANCELLED_BY':
      return `${rest}이(가) 취소`
    case 'MODEL_UNAVAILABLE':
      return `모델 준비 실패: ${rest}`
    case 'EXCEPTION':
      return `예외: ${rest}`
    case 'LLM_CONFIG':
      return `모델 설정 오류: ${rest}`
    case 'ESCALATE_NO_SOLUTION':
      return `해를 찾지 못해 이관: ${rest}`
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
}

/** Validation 대표 상태 배지 (§13). PASS 문구는 "정의된 규칙 검사 통과". */
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
  C04: '시간창',
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
  OBJECTION_DRAFT_PENDING: '이견 제약 초안 확인 대기',
  CANCELLED: '협의 취소',
}

export const AGENT_TYPE: Record<string, string> = {
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
  BUDGET_EXHAUSTED: 'Budget 소진',
  STALE: 'STALE',
  CANCELLED: '취소',
  ERROR: '오류',
}

export const WAIT_KIND: Record<string, string> = {
  CANDIDATE_OUTCOME: '후보 검증·검토 결과 대기',
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
  SOLVE_WITH_SCOPE: '탐색 범위 지정 Solver 실행',
  ESCALATE_NO_SOLUTION: '해 없음 이관',
  LIST_ASSIGNABLE_RESOURCES: '사용 가능 자원 조회',
  TRY_ALTERNATIVE_RESOURCE: '대체 자원 시도',
  ASK_TASK_OWNER: '작업 담당자에게 확인 요청',
}

export const WORK_TYPE: Record<string, string> = {
  LIFTING: '인양',
  WORK_BELOW: '하부 작업',
  HOT_WORK: '화기',
  PAINTING: '도장',
}

export const ROLE: Record<string, string> = {
  UNIT_PLANNER: '공정 담당',
  REPORTER: '현장 신고',
  SUPERVISOR: '현장 감독',
}

export const REJECT_REASON: Record<string, string> = {
  TASK_IMMOVABLE: '작업 이동 불가(제약 생성)',
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
