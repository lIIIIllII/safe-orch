// 받은 요청 수 (부록 A.21 7). 상태바 배지와 입력 영역 탭이 같이 쓴다.

import type { SiteState } from './types'

/** 답할 수 있는 메시지 유형. 통지(NOTICE)는 답을 받지 않으므로 세지 않는다 (A.24). */
export const ANSWERABLE = ['QUESTION', 'CHANGE_REQUEST', 'CONFIRMATION']

export function openInbox(state: SiteState | null): number {
  return (state?.inbox ?? []).filter((m) => m.status === 'OPEN' && ANSWERABLE.includes(m.type)).length
}
