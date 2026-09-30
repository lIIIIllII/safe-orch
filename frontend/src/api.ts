// API 호출 (부록 A.18·A.19). X-Actor는 Actor 전환 값, Idempotency-Key는 사용자 조작마다 새로 만든다.
// 503과 응답을 받지 못한 네트워크 오류는 같은 키로 재시도한다(§12).

import type { AgentStep, CommandResponse, SiteState } from './types'

export const SITE_ID: string = import.meta.env.VITE_SITE_ID ?? 'YARD-01'

const MAX_RETRIES = 3

export function newKey(): string {
  return `ui-${crypto.randomUUID()}`
}

async function getJson<T>(path: string, actor: string): Promise<T> {
  const res = await fetch(`/api${path}`, { headers: { 'X-Actor': actor } })
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    const codes: string[] = body?.reason_codes ?? []
    throw new Error(`${res.status} ${codes.join(', ')}`.trim())
  }
  return (await res.json()) as T
}

export function fetchState(actor: string): Promise<SiteState> {
  return getJson<SiteState>(`/sites/${SITE_ID}/state`, actor)
}

export function fetchSteps(actor: string, runId: string): Promise<AgentStep[]> {
  return getJson<AgentStep[]>(`/runs/${runId}/steps`, actor)
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** 변경 명령. key는 호출자가 조작마다 만든다. 재시도는 같은 key로 한다. */
export async function postCommand(
  path: string,
  actor: string,
  body: unknown,
  key: string | null = newKey(),
): Promise<CommandResponse> {
  let last: CommandResponse | null = null
  let retryAfterMs = 1000
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    if (attempt > 0) await sleep(retryAfterMs)
    retryAfterMs = 1000
    try {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        'X-Actor': actor,
      }
      if (key) headers['Idempotency-Key'] = key
      const res = await fetch(`/api${path}`, {
        method: 'POST',
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      const data = await res.json().catch(() => ({}))
      last = {
        status: data.status ?? 'REJECTED',
        reason_codes: data.reason_codes ?? [],
        context_version: data.context_version ?? null,
        plan_revision: data.plan_revision ?? null,
        result_refs: data.result_refs ?? {},
        detail: data.detail,
        http: res.status,
      }
      if (res.status !== 503) return last
      const ra = Number(res.headers.get('Retry-After'))
      retryAfterMs = Number.isFinite(ra) && ra > 0 ? ra * 1000 : 1000
    } catch {
      last = {
        status: 'NETWORK_ERROR',
        reason_codes: [],
        context_version: null,
        plan_revision: null,
        result_refs: {},
        http: null,
      }
    }
  }
  return last!
}
