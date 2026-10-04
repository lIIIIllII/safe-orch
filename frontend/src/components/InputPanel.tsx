// 입력: 작업 요청 폼, 일정 넣기, 지연 신고, 받은 요청(Inbox), 요청·대기열·Hold 목록.
// 클라이언트는 형식(시각·숫자 변환)만 확인하고 업무 규칙은 서버 판정을 보여 준다.

import { useState } from 'react'
import { newKey } from '../api'
import type { Demand, Requirement, SiteState } from '../types'
import { attributeText, demandText, requirementText, usableInZone, useEnv, workTypeName } from '../context'
import type { Run } from './ReviewPanel'
import { Inbox } from './Inbox'
import { ScheduleImport } from './ScheduleImport'
import { openInbox } from '../inbox'
import { FACT_FIELD, PROPOSAL_STATUS } from '../labels'

interface Props {
  state: SiteState
  actorId: string
  roles: string[]
  busy: string | null
  run: Run
}

export function InputPanel(props: Props) {
  const [tab, setTab] = useState<'task' | 'intake' | 'schedule' | 'event' | 'inbox'>('task')
  const unread = openInbox(props.state)
  return (
    <section className="panel inputs">
      <div className="tabs">
        <button className={tab === 'task' ? 'tab-on' : ''} onClick={() => setTab('task')}>
          작업 요청
        </button>
        <button className={tab === 'intake' ? 'tab-on' : ''} onClick={() => setTab('intake')}>
          자연어 요청
        </button>
        <button className={tab === 'schedule' ? 'tab-on' : ''} onClick={() => setTab('schedule')}>
          일정 넣기
        </button>
        <button className={tab === 'event' ? 'tab-on' : ''} onClick={() => setTab('event')}>
          지연 신고
        </button>
        <button className={tab === 'inbox' ? 'tab-on' : ''} onClick={() => setTab('inbox')}>
          받은 요청 {unread > 0 ? <span className="tab-badge">{unread}</span> : <span className="muted">0</span>}
        </button>
      </div>
      <div className="panel-body">
        {/* 탭을 바꿔도 입력 중인 값을 잃지 않게 둘 다 마운트한다 */}
        <div hidden={tab !== 'task'}>
          <TaskRequestForm {...props} />
        </div>
        <div hidden={tab !== 'intake'}>
          <IntakeForm {...props} />
        </div>
        <div hidden={tab !== 'schedule'}>
          <ScheduleImport {...props} />
        </div>
        <div hidden={tab !== 'event'}>
          <EventForm {...props} />
        </div>
        {tab === 'inbox' ? (
          <Inbox state={props.state} busy={props.busy} run={props.run} />
        ) : (
          <>
            <RequestList {...props} />
            <HoldList {...props} />
          </>
        )}
      </div>
    </section>
  )
}

interface FormState {
  task_id: string
  work_type: string
  zone_id: string
  duration: string
  es_day: string
  es_time: string
  ls_day: string
  ls_time: string
  le_day: string
  le_time: string
  required_resource_type: string
  requested_resource_id: string
  /** 요구 조건 입력 행: 속성 이름 → 비교·값. 값이 비면 보내지 않는다 */
  req: Record<string, { op: string; value: string }>
  /** 수요 입력 행: 풀 종류 → 수량. 비면 보내지 않는다(작업 유형 기본 수요만 쓴다) */
  demand: Record<string, string>
}

type TextField = Exclude<keyof FormState, 'req' | 'demand'>
type TimeField = 'es' | 'ls' | 'le'

function emptyForm(firstDay: string): FormState {
  return {
    task_id: '',
    work_type: '',
    zone_id: '',
    duration: '',
    es_day: firstDay,
    es_time: '',
    ls_day: firstDay,
    ls_time: '',
    le_day: firstDay,
    le_time: '',
    required_resource_type: '',
    requested_resource_id: '',
    req: {},
    demand: {},
  }
}

/** 작업 요청 폼. 시각은 근무일 select + HH:MM, 옆에 "= N분". 근무시간 밖은 막지 않고 안내만 한다. */
function TaskRequestForm({ state, actorId, roles, busy, run }: Props) {
  const { siteId, meta, clock, scenario } = useEnv()
  const firstDay = clock.workDays[0]?.key ?? ''
  const [f, setF] = useState<FormState>(() => emptyForm(firstDay))
  const [demo, setDemo] = useState<number | null>(null)
  const allowed = roles.includes('UNIT_PLANNER')
  const resourceTypes = [...new Set(meta.resources.map((r) => r.resource_type))]
  const actorName = new Map(state.actors.map((a) => [a.actor_id, a.name]))
  const set = (k: TextField) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })
  // 요구 조건 입력 행은 속성 선언(meta.resource_attributes)으로 만든다. 수치는 ≥·≤, 목록은 포함
  const reqRow = (name: string) => f.req[name] ?? { op: 'GTE', value: '' }
  const setReq = (name: string, change: Partial<{ op: string; value: string }>) =>
    setF({ ...f, req: { ...f.req, [name]: { ...reqRow(name), ...change } } })
  const requirements: Requirement[] = meta.resource_attributes.flatMap((a): Requirement[] => {
    const row = reqRow(a.name)
    const text = row.value.trim()
    if (!text) return []
    if (a.type === 'LIST') return [{ attribute: a.name, op: 'CONTAINS', value: text }]
    const n = Number(text)
    return Number.isFinite(n) ? [{ attribute: a.name, op: row.op === 'LTE' ? 'LTE' : 'GTE', value: n }] : []
  })
  const defaults = meta.work_types[f.work_type]?.resource_requirements ?? []
  // 수요 입력 행은 풀 종류 선언(meta.pool_kinds)으로 만든다
  const demands: Demand[] = meta.pool_kinds.flatMap((k): Demand[] => {
    const n = Number((f.demand[k.kind] ?? '').trim() || NaN)
    return Number.isInteger(n) && n > 0 ? [{ kind: k.kind, quantity: n }] : []
  })
  const defaultDemands = meta.work_types[f.work_type]?.pool_demands ?? []
  const chosen = meta.resources.find((r) => r.resource_id === f.requested_resource_id)
  const minute = (field: TimeField): number | null => {
    const time = f[`${field}_time`]
    return time.trim() ? clock.toMinute(f[`${field}_day`], time) : null
  }
  const hint = (field: TimeField) => {
    if (!f[`${field}_time`].trim()) return ''
    const m = minute(field)
    return m === null ? '형식 HH:MM' : `= ${m}분`
  }
  // 근무일 select: 근무 구간의 날짜. 시연값이 근무일이 아닌 날짜를 쓰면 그 날짜도 넣는다.
  const dayOptions = (value: string) => {
    const opts = clock.workDays.map((d) => ({ key: d.key, label: d.label }))
    return opts.some((o) => o.key === value) || !value ? opts : [...opts, { key: value, label: value }]
  }
  const timeInput = (field: TimeField, label: string) => (
    <label>
      {label} <span className="hint">{hint(field)}</span>
      <span className="row tight">
        <select value={f[`${field}_day`]} onChange={set(`${field}_day`)}>
          {dayOptions(f[`${field}_day`]).map((o) => (
            <option key={o.key} value={o.key}>
              {o.label}
            </option>
          ))}
        </select>
        <input className="time" placeholder="HH:MM" value={f[`${field}_time`]} onChange={set(`${field}_time`)} />
      </span>
    </label>
  )
  const fillDemo = (i: number) => {
    const req = scenario?.task_requests[i]
    if (!req) return
    const r = req.form
    const at = (m: number) => ({ day: clock.dateKey(m), time: clock.hm(m) })
    const [es, ls, le] = [at(r.earliest_start), at(r.latest_start), at(r.latest_end)]
    setDemo(i)
    setF({
      task_id: r.task_id,
      work_type: r.work_type,
      zone_id: r.zone_id,
      duration: String(r.duration),
      es_day: es.day,
      es_time: es.time,
      ls_day: ls.day,
      ls_time: ls.time,
      le_day: le.day,
      le_time: le.time,
      required_resource_type: r.required_resource_type ?? '',
      requested_resource_id: r.requested_resource_id ?? '',
      req: Object.fromEntries(
        (r.resource_requirements ?? []).map((q) => [q.attribute, { op: q.op, value: String(q.value) }]),
      ),
      demand: Object.fromEntries((r.pool_demands ?? []).map((d) => [d.kind, String(d.quantity)])),
    })
  }
  const demoReq = demo === null ? null : (scenario?.task_requests[demo] ?? null)
  const openRun = state.runs.some(
    (r) => r.agent_type === 'MAIN' && (r.status === 'RUNNING' || r.status === 'WAITING_HUMAN'),
  )
  const queued = state.task_queue.length
  const es = minute('es')
  const dur = f.duration.trim() ? Number(f.duration) : null
  const outside = es !== null && dur !== null && Number.isFinite(dur) && !clock.inWork(es, es + dur)
  const submit = () => {
    const body = {
      task_id: f.task_id,
      work_type: f.work_type,
      zone_id: f.zone_id,
      duration: dur,
      earliest_start: es,
      latest_start: minute('ls'),
      latest_end: minute('le'),
      required_resource_type: f.required_resource_type || null,
      requested_resource_id: f.requested_resource_id || null,
      resource_requirements: requirements,
      pool_demands: demands,
      predecessors: [],
    }
    void run('작업 요청', `/sites/${siteId}/task-requests`, body)
  }
  return (
    <div className="form-grid">
      {scenario && (
        <label className="span2">
          시연값 ▾
          <select
            value={demo === null ? '' : String(demo)}
            onChange={(e) => (e.target.value === '' ? setDemo(null) : fillDemo(Number(e.target.value)))}
          >
            <option value="">선택 안 함</option>
            {scenario.task_requests.map((r, i) => (
              <option key={r.form.task_id} value={i}>
                {r.form.task_id} · {r.label} — 요청자 {actorName.get(r.requester) ?? r.requester}
              </option>
            ))}
          </select>
        </label>
      )}
      <label>
        작업 ID
        <input value={f.task_id} onChange={set('task_id')} />
      </label>
      <label>
        작업 유형
        <select value={f.work_type} onChange={set('work_type')}>
          <option value="">선택</option>
          {Object.entries(meta.work_types).map(([w, wt]) => (
            <option key={w} value={w}>
              {wt.display_name} ({w})
            </option>
          ))}
        </select>
      </label>
      <label>
        구역
        <select value={f.zone_id} onChange={set('zone_id')}>
          <option value="">선택</option>
          {meta.zones.map((z) => (
            <option key={z} value={z}>
              {z}
            </option>
          ))}
        </select>
      </label>
      <label>
        소요(분)
        <input inputMode="numeric" value={f.duration} onChange={set('duration')} />
      </label>
      <p className="muted small span2">
        근무시간{' '}
        {clock.workDays.map((d) => `${d.label} ${clock.hm(d.lo)}–${clock.hm(d.hi)}`).join(' · ')}
      </p>
      {timeInput('es', '시작 가능')}
      {timeInput('ls', '시작 늦어도')}
      {timeInput('le', '종료 늦어도')}
      <label>
        필요 자원 유형
        <select value={f.required_resource_type} onChange={set('required_resource_type')}>
          <option value="">없음</option>
          {resourceTypes.map((r) => (
            <option key={r} value={r}>
              {meta.resource_types[r] ?? r} ({r})
            </option>
          ))}
        </select>
      </label>
      <label>
        요청 자원
        <select value={f.requested_resource_id} onChange={set('requested_resource_id')}>
          <option value="">없음</option>
          {meta.resources.map((r) => {
            // 구역이 안 맞는 자원은 사유와 함께 비활성. 나머지 조건은 서버가 판정한다
            const off = f.zone_id !== '' && !usableInZone(r, f.zone_id)
            const attrs = attributeText(meta, r)
            return (
              <option key={r.resource_id} value={r.resource_id} disabled={off}>
                {r.resource_id} · {r.display_name || r.resource_type}
                {attrs && ` (${attrs})`}
                {off && ` — ${f.zone_id} 구역에서 쓸 수 없음`}
              </option>
            )
          })}
        </select>
      </label>
      {chosen && (
        <p className="muted small span2">
          {chosen.resource_id} 사용 가능 구역{' '}
          {chosen.allowed_zone_ids.includes('*') ? '모든 구역' : chosen.allowed_zone_ids.join(', ')}
          {chosen.note && ` · ${chosen.note}`}
        </p>
      )}
      {chosen && f.zone_id !== '' && !usableInZone(chosen, f.zone_id) && (
        <p className="warn small span2">
          {chosen.resource_id}은(는) {f.zone_id} 구역에서 쓸 수 없습니다. 제출하면 서버가 거절합니다.
        </p>
      )}
      {f.required_resource_type !== '' &&
        meta.resource_attributes.map((a) => (
          <label key={a.name}>
            자원 요구 조건 · {a.display_name}
            <span className="row tight">
              {a.type === 'NUMBER' ? (
                <select value={reqRow(a.name).op} onChange={(e) => setReq(a.name, { op: e.target.value })}>
                  <option value="GTE">≥ 이상</option>
                  <option value="LTE">≤ 이하</option>
                </select>
              ) : (
                <span className="hint">포함</span>
              )}
              <input
                inputMode={a.type === 'NUMBER' ? 'decimal' : undefined}
                placeholder="없음"
                value={reqRow(a.name).value}
                onChange={(e) => setReq(a.name, { value: e.target.value })}
              />
              {a.unit && <span className="hint">{a.unit}</span>}
            </span>
          </label>
        ))}
      {f.required_resource_type !== '' && defaults.length > 0 && (
        <p className="muted small span2">
          작업 유형 기본 요구 조건(서버가 붙임): {defaults.map((q) => requirementText(meta, q)).join(', ')}. 입력한
          조건은 여기에 더해집니다.
        </p>
      )}
      {meta.pool_kinds.map((k) => (
        <label key={k.kind}>
          수요 · {k.display_name}
          <span className="row tight">
            <input
              inputMode="numeric"
              placeholder="기본값"
              value={f.demand[k.kind] ?? ''}
              onChange={(e) => setF({ ...f, demand: { ...f.demand, [k.kind]: e.target.value } })}
            />
            {k.unit && <span className="hint">{k.unit}</span>}
          </span>
        </label>
      ))}
      {defaultDemands.length > 0 && (
        <p className="muted small span2">
          작업 유형 기본 수요(서버가 붙임): {defaultDemands.map((d) => demandText(meta, d)).join(', ')}. 입력한 수량이
          더 클 때만 반영됩니다.
        </p>
      )}
      {outside && (
        <p className="warn small span2">
          요청 시작이 근무시간 밖입니다. 접수되면 근무시간 충돌(CALENDAR)로 재계획되고, 시간창에 근무시간 자리가 없으면
          서버가 거절합니다.
        </p>
      )}
      {demoReq && demoReq.requester !== actorId && (
        <p className="warn small span2">
          이 시연값의 요청자는 {actorName.get(demoReq.requester) ?? demoReq.requester}입니다. 현재 Actor로 보내면 그
          Actor의 작업이 됩니다(경고만).
        </p>
      )}
      {(openRun || queued > 0) && (
        <p className="warn small span2">
          {openRun ? '열린 재계획 Run이 있어' : `대기열에 요청 ${queued}건이 있어`} 지금 보내는 요청은 대기열에
          접수됩니다. 앞 Case가 끝나면 접수 순서대로 재검사됩니다.
        </p>
      )}
      <div className="row span2">
        <button
          type="button"
          onClick={() => {
            setF(emptyForm(firstDay))
            setDemo(null)
          }}
        >
          비우기
        </button>
        <span className="spacer" />
        <button
          className="btn-primary"
          disabled={!allowed || busy !== null}
          title={allowed ? undefined : '공정 담당(UNIT_PLANNER) 권한 필요'}
          onClick={submit}
        >
          요청 제출
        </button>
      </div>
      {!allowed && <p className="muted small span2">공정 담당(UNIT_PLANNER) 권한 필요</p>}
    </div>
  )
}

/** Plan 밖 READY 작업(해결 전 요청)·대기열(QUEUED)과 철회.
 * 작업 담당자나 SUPERVISOR만 버튼이 켜진다. 대기열은 접수 순서(state.task_queue)로 "대기 n번째". */
function RequestList({ state, actorId, roles, busy, run }: Props) {
  const { meta, clock } = useEnv()
  const inPlan = new Set(state.plan.assignments.map((a) => a.task_id))
  const byId = new Map(state.tasks.map((t) => [t.task_id, t]))
  const pending = [
    ...state.tasks.filter((t) => t.lifecycle === 'READY' && !inPlan.has(t.task_id)),
    ...state.task_queue.flatMap((id) => byId.get(id) ?? []),
  ]
  const name = new Map(state.actors.map((a) => [a.actor_id, a.name]))
  const isSup = roles.includes('SUPERVISOR')
  return (
    <div className="requests">
      <h3>
        요청 (Plan 밖) {pending.length}건
        {state.task_queue.length > 0 && <span className="muted small"> · 대기열 {state.task_queue.length}건</span>}
      </h3>
      {pending.length === 0 && <p className="muted">없음</p>}
      {pending.map((t) => {
        const can = isSup || t.owner_actor_id === actorId
        const place = state.task_queue.indexOf(t.task_id)
        return (
          <div key={t.task_id} className="row request">
            <span className="grow">
              {place >= 0 && <span className="badge badge-queued">대기 {place + 1}번째</span>}{' '}
              <b>{t.task_id}</b> {workTypeName(meta, t.work_type)} · {t.zone_id} ·{' '}
              {clock.span(t.earliest_start, t.earliest_start + t.duration)}
              <span className="muted small"> — {name.get(t.owner_actor_id) ?? t.owner_actor_id}</span>
            </span>
            <button
              disabled={!can || busy !== null}
              title={can ? '계산 대상에서 뺍니다(NEEDS_INFO)' : '작업 담당자 또는 Supervisor 권한 필요'}
              onClick={() => run(`요청 ${t.task_id} 철회`, `/tasks/${t.task_id}/withdraw`, { comment: '' })}
            >
              철회
            </button>
          </div>
        )
      })}
    </div>
  )
}

function IntakeForm({ actorId, roles, busy, run }: Props) {
  // 자연어 작업 요청 → Work Intake Agent. 묻지 않고 완료한다. 문장의 시각은 희망 영역이 된다(가능 범위는 폼·카드로).
  const { siteId, scenario } = useEnv()
  const [taskId, setTaskId] = useState('')
  const [text, setText] = useState('')
  const [requester, setRequester] = useState<string | null>(null)
  const allowed = roles.includes('UNIT_PLANNER')
  return (
    <div className="form-grid">
      <label>
        작업 ID
        <input value={taskId} onChange={(e) => setTaskId(e.target.value)} />
      </label>
      <p className="muted small">
        문장의 시각은 희망 영역이 됩니다(벗어난 안도 나올 수 있고, 말한 범위 밖이면 물어봅니다). 반드시 지켜야 하는
        범위는 폼 탭이나 작업 카드의 값 고치기로 넣습니다.
      </p>
      <label className="span2">
        요청 문장
        <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <div className="row span2">
        {/* 시연 문장은 scenario(DEMO_MODE)에서만 받는다 */}
        {scenario?.intake_requests?.map((x) => (
          <button
            key={x.label}
            type="button"
            onClick={() => {
              setTaskId(x.body.task_id)
              setText(x.body.text)
              setRequester(x.requester)
            }}
          >
            시연: {x.label}
          </button>
        ))}
        <span className="spacer" />
        <button
          className="btn-primary"
          disabled={!allowed || busy !== null || !taskId.trim() || !text.trim()}
          title={allowed ? undefined : '공정 담당(UNIT_PLANNER) 권한 필요'}
          onClick={() => void run('자연어 요청', `/sites/${siteId}/intakes`, { task_id: taskId, text })}
        >
          요청 보내기
        </button>
      </div>
      {requester && requester !== actorId && (
        <p className="warn small span2">시연값의 요청자는 {requester}입니다. 현재 Actor와 다릅니다(자동 전환하지 않음).</p>
      )}
      {!allowed && <p className="muted small span2">공정 담당(UNIT_PLANNER) 권한 필요</p>}
    </div>
  )
}

function EventForm({ state, roles, busy, run }: Props) {
  const { siteId, meta, scenario } = useEnv()
  const [type, setType] = useState<'DELAY' | 'OTHER'>('DELAY')
  const [target, setTarget] = useState('')
  const [text, setText] = useState('')
  const allowed = roles.includes('REPORTER') || roles.includes('SUPERVISOR')
  const submit = () => {
    // source_event_id는 제출마다 새로. 503 재시도는 run 안에서 같은 본문·같은 키로 한다.
    const sourceId = newKey()
    void run('지연 신고', `/sites/${siteId}/events`, {
      source_event_id: sourceId,
      event_type: type,
      text,
      target_task_id: target || null,
    })
  }
  return (
    <div className="form-grid">
      <label>
        유형
        <select value={type} onChange={(e) => setType(e.target.value as 'DELAY' | 'OTHER')}>
          <option value="DELAY">지연 (DELAY)</option>
          <option value="OTHER">기타 (OTHER)</option>
        </select>
      </label>
      <label>
        대상 작업
        <select value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">지정 안 함 (현장 전체 Hold)</option>
          {state.tasks.map((t) => (
            <option key={t.task_id} value={t.task_id}>
              {t.task_id} {workTypeName(meta, t.work_type)}
            </option>
          ))}
        </select>
      </label>
      <label className="span2">
        신고 내용
        <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <div className="row span2">
        {/* 신고 문구는 scenario(DEMO_MODE)에서만 받는다 */}
        {scenario?.event_reports.map((e) => (
          <button
            key={e.label}
            type="button"
            onClick={() => {
              setType(e.body.event_type)
              setText(e.body.text)
              setTarget(e.body.target_task_id ?? '')
            }}
          >
            시연: {e.label}
          </button>
        ))}
        <span className="spacer" />
        <button
          className="btn-warn"
          disabled={!allowed || busy !== null}
          title={allowed ? undefined : '현장 신고(REPORTER) 또는 Supervisor 권한 필요'}
          onClick={submit}
        >
          신고 제출
        </button>
      </div>
      {!allowed && <p className="muted small span2">현장 신고(REPORTER) 또는 Supervisor 권한 필요</p>}
    </div>
  )
}

function HoldList({ state, roles, busy, run }: Props) {
  const isSup = roles.includes('SUPERVISOR')
  const { clock } = useEnv()
  const [comment, setComment] = useState('')
  const name = new Map(state.actors.map((a) => [a.actor_id, a.name]))
  return (
    <div className="holds">
      <h3>ACTIVE Hold {state.holds.length}건</h3>
      {state.holds.length === 0 && <p className="muted">없음</p>}
      {state.holds.map((h) => (
        <div key={h.hold_id} className="hold">
          <div>
            <span className="badge badge-hold">{h.scope === 'SITE' ? '현장 전체' : `작업 ${h.task_id}`}</span>{' '}
            “{h.text}” <span className="muted small">— {name.get(h.reporter_actor_id) ?? h.reporter_actor_id}</span>
          </div>
          <div className="small muted">
            {h.hold_id} · 생성 Context v{h.created_context_version} · {h.event_type}
          </div>
          {h.fact_updates.length > 0 && (
            <ul className="small hold-facts">
              {h.fact_updates.map((f) => (
                <li key={f.proposal_id}>
                  사실 수정안 {f.task_id} {FACT_FIELD[f.field] ?? f.field} {clock.format(f.old_value)} →{' '}
                  <b>{clock.format(f.new_value)}</b> · {PROPOSAL_STATUS[f.status] ?? f.status}
                </li>
              ))}
            </ul>
          )}
          <p className="small hold-note">
            해제 사유: <b>변경 없음(NO_CHANGE)</b> 또는 <b>사실 확인(FACT_CONFIRMED)</b>
            <span className="muted"> · 사실 확인 해제는 이 신고의 사실 수정이 확정된 뒤에만 된다</span>
          </p>
          <div className="row">
            <input
              className="grow"
              placeholder="해제 메모(선택)"
              value={comment}
              onChange={(e) => setComment(e.target.value)}
            />
            <button
              disabled={!isSup || busy !== null}
              title={isSup ? undefined : 'Supervisor 권한 필요'}
              onClick={() =>
                run('Hold 해제(변경 없음)', `/holds/${h.hold_id}/release`, {
                  resolution: 'NO_CHANGE',
                  expected_context_version: state.site.context_version,
                  comment,
                })
              }
            >
              변경 없음 해제
            </button>
            <button
              className="btn-primary"
              disabled={!isSup || busy !== null}
              title={isSup ? '판정은 서버가 한다(확정 전이면 FACT_NOT_CONFIRMED)' : 'Supervisor 권한 필요'}
              onClick={() =>
                run('Hold 해제(사실 확인)', `/holds/${h.hold_id}/release`, {
                  resolution: 'FACT_CONFIRMED',
                  expected_context_version: state.site.context_version,
                  comment,
                })
              }
            >
              사실 확인 후 해제
            </button>
          </div>
        </div>
      ))}
      <details>
        <summary className="small">최근 신고 {state.events.length}건</summary>
        <ul className="small">
          {state.events.map((e) => (
            <li key={e.event_id}>
              {e.event_type} “{e.text}” · {name.get(e.reporter_actor_id) ?? e.reporter_actor_id} · 대상{' '}
              {e.target_task_id ?? '없음'} · Hold {e.hold_status === 'ACTIVE' ? '유지' : '해제됨'} · Context v
              {e.context_version}
            </li>
          ))}
        </ul>
      </details>
    </div>
  )
}
