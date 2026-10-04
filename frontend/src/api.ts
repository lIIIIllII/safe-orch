// API 호출. X-Actor는 Actor 전환 값, Idempotency-Key는 사용자 조작마다 새로 만든다.
// 503과 응답을 받지 못한 네트워크 오류는 같은 키로 재시도한다.

import type {
  AgentStep,
  CommandResponse,
  Meta,
  MoveCheck,
  MoveOptions,
  RemoveCheck,
  ResourceCheck,
  Scenario,
  SchedulePreview,
  SiteEntry,
  SiteState,
} from './types'

const MAX_RETRIES = 3

export function newKey(): string {
  return `ui-${crypto.randomUUID()}`
}

class HttpError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function getJson<T>(path: string, actor: string | null): Promise<T> {
  const res = await fetch(`/api${path}`, { headers: actor ? { 'X-Actor': actor } : {} })
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    const codes: string[] = body?.reason_codes ?? []
    throw new HttpError(res.status, `${res.status} ${codes.join(', ')}`.trim())
  }
  return (await res.json()) as T
}

/** 현장 목록과 Actor. X-Actor 없이 읽는다. site_id는 여기서 받는다. */
export async function fetchSites(): Promise<SiteEntry[]> {
  return (await getJson<{ sites: SiteEntry[] }>('/sites', null)).sites
}

export function fetchMeta(siteId: string, actor: string): Promise<Meta> {
  return getJson<Meta>(`/sites/${siteId}/meta`, actor)
}

/** 시연값. DEMO_MODE가 아니면(404) null. */
export async function fetchScenario(actor: string): Promise<Scenario | null> {
  try {
    return await getJson<Scenario>('/dev/scenario', actor)
  } catch (e) {
    if (e instanceof HttpError && e.status === 404) return null
    throw e
  }
}

export function fetchState(siteId: string, actor: string): Promise<SiteState> {
  return getJson<SiteState>(`/sites/${siteId}/state`, actor)
}

export function fetchSteps(actor: string, runId: string): Promise<AgentStep[]> {
  return getJson<AgentStep[]>(`/runs/${runId}/steps`, actor)
}

/** 직접 이동: 그 작업을 놓을 수 있는 시작 구간(서버 계산). 끌기를 시작할 때 한 번 받는다. */
export function fetchMoveRange(actor: string, taskId: string): Promise<MoveOptions> {
  return getJson<MoveOptions>(`/tasks/${encodeURIComponent(taskId)}/move-range`, actor)
}

/** 직접 이동: 놓은 자리의 서버 판정. 확정과 같은 판정이다. */
export function fetchMoveCheck(actor: string, taskId: string, start: number): Promise<MoveCheck> {
  return getJson<MoveCheck>(`/tasks/${encodeURIComponent(taskId)}/move-check?start=${start}`, actor)
}

/** 작업 없애기: 없앨 수 있는지와 무효가 될 검토 중인 안(서버 판정). 계획 밖 요청이면 철회 경로다. */
export function fetchRemoveCheck(actor: string, taskId: string): Promise<RemoveCheck> {
  return getJson<RemoveCheck>(`/tasks/${encodeURIComponent(taskId)}/remove-check`, actor)
}

/** 작업 카드의 자원 바꾸기: 그 자원으로 바꿀 수 있는지와 무효가 될 검토 중인 안(서버 판정). */
export function fetchResourceCheck(actor: string, taskId: string, resourceId: string): Promise<ResourceCheck> {
  const id = encodeURIComponent(taskId)
  return getJson<ResourceCheck>(`/tasks/${id}/resource-check?resource_id=${encodeURIComponent(resourceId)}`, actor)
}

/** 기록에 남은 일정 문서 원문. 꺼내기 명령이 돌려준 schedule_id로 받는다. */
export function fetchSchedule(actor: string, scheduleId: string): Promise<unknown> {
  return getJson<unknown>(`/schedules/${encodeURIComponent(scheduleId)}`, actor)
}

/** 일정 넣기 미리보기: 작업별 판정(서버 계산). 읽기 전용이라 Idempotency-Key가 없다. */
export async function fetchSchedulePreview(
  siteId: string,
  actor: string,
  document: unknown,
  exclude: string[],
): Promise<SchedulePreview> {
  const res = await fetch(`/api/sites/${siteId}/schedules/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Actor': actor },
    body: JSON.stringify({ document, exclude }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    const codes: string[] = body?.reason_codes ?? []
    throw new HttpError(res.status, `${res.status} ${codes.join(', ')}`.trim())
  }
  return (await res.json()) as SchedulePreview
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
