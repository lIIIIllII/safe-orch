// 받은 요청 수 (부록 A.21 7). 상태바 배지와 입력 영역 탭이 같이 쓴다.

import type { SiteState } from './types'

export function openInbox(state: SiteState | null): number {
  return (state?.inbox ?? []).filter((m) => m.status === 'OPEN').length
}
