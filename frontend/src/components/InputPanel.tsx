// 입력: 작업 요청 폼, 지연 신고, Hold 목록 (§13, 부록 A.14·A.19).
// 클라이언트는 형식(시각·숫자 변환)만 확인하고 업무 규칙은 서버 판정을 보여 준다.

import { useState } from 'react'
import { SITE_ID, newKey } from '../api'
import type { SiteState } from '../types'
import { WORK_TYPE } from '../labels'
import { clockToMinute, minuteToClock } from '../time'
import type { Run } from './ReviewPanel'

interface Props {
  state: SiteState
  roles: string[]
  busy: string | null
  run: Run
}

export function InputPanel(props: Props) {
  const [tab, setTab] = useState<'task' | 'event'>('task')
  return (
    <section className="panel inputs">
      <div className="tabs">
        <button className={tab === 'task' ? 'tab-on' : ''} onClick={() => setTab('task')}>
          작업 요청
        </button>
        <button className={tab === 'event' ? 'tab-on' : ''} onClick={() => setTab('event')}>
          지연 신고
        </button>
      </div>
      <div className="panel-body">
        {/* 탭을 바꿔도 입력 중인 값을 잃지 않게 둘 다 마운트한다 */}
        <div hidden={tab !== 'task'}>
          <TaskRequestForm {...props} />
        </div>
        <div hidden={tab !== 'event'}>
          <EventForm {...props} />
        </div>
        <HoldList {...props} />
      </div>
    </section>
  )
}

const EMPTY_FORM = {
  task_id: '',
  work_type: '',
  zone_id: '',
  duration: '',
  earliest_start: '',
  latest_start: '',
  latest_end: '',
  required_resource_type: '',
  requested_resource_id: '',
}

const DEMO_A = {
  task_id: 'A',
  work_type: 'LIFTING',
  zone_id: 'B',
  duration: '30',
  earliest_start: '09:00',
  latest_start: '10:00',
  latest_end: '10:30',
  required_resource_type: 'CRANE',
  requested_resource_id: 'A-CR-01',
}

function TaskRequestForm({ state, roles, busy, run }: Props) {
  const [f, setF] = useState(EMPTY_FORM)
  const origin = state.site.horizon_start_utc
  const allowed = roles.includes('UNIT_PLANNER')
  // Pack work_type은 state에 없으므로 현재 작업의 work_type과 시연 A의 LIFTING을 합친다 (부록 A.19)
  const workTypes = [...new Set(['LIFTING', ...state.tasks.map((t) => t.work_type)])]
  const resourceTypes = [...new Set(state.resources.map((r) => r.resource_type))]
  const set = (k: keyof typeof EMPTY_FORM) => (e: { target: { value: string } }) =>
    setF({ ...f, [k]: e.target.value })
  const clock = (v: string) => (v.trim() ? clockToMinute(origin, v) : null)
  const hint = (v: string) => {
    const m = clock(v)
    return v.trim() ? (m === null ? '형식 HH:MM' : `= ${m}분`) : ''
  }
  const submit = () => {
    const body = {
      task_id: f.task_id,
      work_type: f.work_type,
      zone_id: f.zone_id,
      duration: f.duration.trim() ? Number(f.duration) : null,
      earliest_start: clock(f.earliest_start),
      latest_start: clock(f.latest_start),
      latest_end: clock(f.latest_end),
      required_resource_type: f.required_resource_type || null,
      requested_resource_id: f.requested_resource_id || null,
      predecessors: [],
    }
    void run('작업 요청', `/sites/${SITE_ID}/task-requests`, body)
  }
  return (
    <div className="form-grid">
      <label>
        작업 ID
        <input value={f.task_id} onChange={set('task_id')} />
      </label>
      <label>
        작업 유형
        <select value={f.work_type} onChange={set('work_type')}>
          <option value="">선택</option>
          {workTypes.map((w) => (
            <option key={w} value={w}>
              {WORK_TYPE[w] ?? w} ({w})
            </option>
          ))}
        </select>
      </label>
      <label>
        구역
        <select value={f.zone_id} onChange={set('zone_id')}>
          <option value="">선택</option>
          {state.zones.map((z) => (
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
      <label>
        시작 가능 {minuteToClock(origin, 0)}~ <span className="hint">{hint(f.earliest_start)}</span>
        <input placeholder="HH:MM" value={f.earliest_start} onChange={set('earliest_start')} />
      </label>
      <label>
        시작 늦어도 <span className="hint">{hint(f.latest_start)}</span>
        <input placeholder="HH:MM" value={f.latest_start} onChange={set('latest_start')} />
      </label>
      <label>
        종료 늦어도 <span className="hint">{hint(f.latest_end)}</span>
        <input placeholder="HH:MM" value={f.latest_end} onChange={set('latest_end')} />
      </label>
      <label>
        필요 자원 유형
        <select value={f.required_resource_type} onChange={set('required_resource_type')}>
          <option value="">없음</option>
          {resourceTypes.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </label>
      <label>
        요청 자원
        <select value={f.requested_resource_id} onChange={set('requested_resource_id')}>
          <option value="">없음</option>
          {state.resources.map((r) => (
            <option key={r.resource_id} value={r.resource_id}>
              {r.resource_id}
            </option>
          ))}
        </select>
      </label>
      <div className="row span2">
        <button type="button" onClick={() => setF(DEMO_A)}>
          시연값 A 채우기
        </button>
        <button type="button" onClick={() => setF(EMPTY_FORM)}>
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

function EventForm({ state, roles, busy, run }: Props) {
  const [type, setType] = useState<'DELAY' | 'OTHER'>('DELAY')
  const [target, setTarget] = useState('')
  const [text, setText] = useState('')
  const allowed = roles.includes('REPORTER') || roles.includes('SUPERVISOR')
  const submit = () => {
    // source_event_id는 제출마다 새로. 503 재시도는 run 안에서 같은 본문·같은 키로 한다.
    const sourceId = newKey()
    void run('지연 신고', `/sites/${SITE_ID}/events`, {
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
              {t.task_id} {WORK_TYPE[t.work_type] ?? t.work_type}
            </option>
          ))}
        </select>
      </label>
      <label className="span2">
        신고 내용
        <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <div className="row span2">
        <button type="button" onClick={() => setText('도장 준비 15분 늦어져 10시부터')}>
          시연 문구
        </button>
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
          <p className="small hold-note">
            해제 사유 <b>변경 없음 (NO_CHANGE)</b>{' '}
            <span className="muted">· 사실 확정(FACT_CONFIRMED)은 D5 이후</span>
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
                run('Hold 해제', `/holds/${h.hold_id}/release`, {
                  resolution: 'NO_CHANGE',
                  expected_context_version: state.site.context_version,
                  comment,
                })
              }
            >
              해제
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
