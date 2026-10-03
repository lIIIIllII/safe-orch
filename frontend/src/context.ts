// 화면 공통 환경: site_id, Pack 표시 정보(meta), 현장 시계, 시연값.
// 화면 코드는 Pack 값을 상수로 두지 않고 여기서만 읽는다.

import { createContext, useContext } from 'react'
import type { Clock } from './time'
import type { Meta, Requirement, Resource, Scenario } from './types'

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

export function attributeName(meta: Meta, attribute: string): string {
  return meta.resource_attributes.find((a) => a.name === attribute)?.display_name ?? attribute
}

/** 요구 조건 하나의 문구. 속성 표시 이름·단위는 Pack 선언에서 읽는다. */
export function requirementText(meta: Meta, req: Requirement): string {
  const decl = meta.resource_attributes.find((a) => a.name === req.attribute)
  const name = decl?.display_name ?? req.attribute
  if (req.op === 'CONTAINS') return `${name} ${req.value} 포함`
  const unit = decl?.unit ? ` ${decl.unit}` : ''
  return `${name} ${req.op === 'GTE' ? '≥' : '≤'} ${req.value}${unit}`
}

/** 자원의 속성 값 문구 (선언 순서). 값이 없는 속성은 뺀다. */
export function attributeText(meta: Meta, resource: Resource): string {
  return meta.resource_attributes
    .flatMap((a) => {
      const v = resource.attributes[a.name]
      if (v === undefined) return []
      const value = Array.isArray(v) ? v.join('·') : `${v}${a.unit ? ` ${a.unit}` : ''}`
      return [`${a.display_name} ${value}`]
    })
    .join(', ')
}

/** 자원을 그 구역에서 쓸 수 있는가 (표시용. 판정은 서버가 한다). */
export function usableInZone(resource: Resource, zoneId: string): boolean {
  return resource.allowed_zone_ids.includes('*') || resource.allowed_zone_ids.includes(zoneId)
}
