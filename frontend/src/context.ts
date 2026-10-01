// 화면 공통 환경: site_id, Pack 표시 정보(meta), 현장 시계, 시연값 (부록 A.20 2차).
// 화면 코드는 Pack 값을 상수로 두지 않고 여기서만 읽는다.

import { createContext, useContext } from 'react'
import type { Clock } from './time'
import type { Meta, Scenario } from './types'

export interface Env {
  siteId: string
  meta: Meta
  clock: Clock
  /** DEMO_MODE가 아니면 null이고 시연 메뉴를 숨긴다. */
  scenario: Scenario | null
}

export const EnvContext = createContext<Env | null>(null)

export function useEnv(): Env {
  const env = useContext(EnvContext)
  if (!env) throw new Error('EnvContext missing')
  return env
}

/** Rule·기본 제약 표시 이름: Pack Rule은 meta, 나머지는 공통 코드표. */
export function ruleName(meta: Meta, code: string): string | undefined {
  return meta.rules.find((r) => r.rule_id === code)?.display_name
}

export function workTypeName(meta: Meta, workType: string): string {
  return meta.work_types[workType]?.display_name ?? workType
}
