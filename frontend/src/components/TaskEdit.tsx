// 작업 카드의 "Agent가 정한 값"과 값 고치기·확인(AG-33). 담당자(요청자)만 한다.
// Work Intake가 정한 값(문장에 없거나 해석·추정이 들어간 값)을 따로 보여 주고, 담당자가 고치거나 그대로 확인한다.
// 고친 값과 확인한 값은 사람이 말한 값이 된다. 검증은 서버가 폼과 같은 규칙으로 한다(화면은 판정하지 않는다).
// 카드에서 고치는 시각은 가능 범위(Hard)다. 희망 영역은 타임라인에서 그리고 지우며, 접수 Agent가 정한 희망은
// 여기 [정한 값 확인]으로 말한 희망이 된다(그때부터 그 범위 안은 묻지 않는다).
// 자원 바꾸기: 서버가 그 자원으로 바꿀 수 있는지 확인해 주고, [확정]하면 계획에 있는 작업은 지금 시각 그대로
// 자원만 바뀐 계획이 바로 확정된다(직접 이동과 같은 판정). 계획 밖 요청은 요청 자원만 고친다.

import { useState } from 'react'
import { fetchResourceCheck } from '../api'
import { decidedValues, ruleName, useEnv, workTypeName } from '../context'
import { REASON, VALUE_NAME } from '../labels'
import type { ResourceCheck, SiteState, Task } from '../types'
import type { Run } from './ReviewPanel'

type TimeKey = 'earliest_start' | 'latest_start' | 'latest_end'
const TIME_KEYS: TimeKey[] = ['earliest_start', 'latest_start', 'latest_end']

interface Props {
  task: Task
  state: SiteState
  /** 지금 Actor가 이 작업의 담당자인가(권한 안내용, 판정은 서버) */
  owner: boolean
  actorId: string
  /** 계획에 있는 작업이면 지금 배정된 자원. 계획 밖이면 undefined */
  planned: { resourceId: string | null } | undefined
  busy: string | null
  run: Run
}

export function TaskEdit({ task, state, owner, actorId, planned, busy, run }: Props) {
  const { clock, meta } = useEnv()
  const decided = decidedValues(task)
  const hope = task.preferred_window
  const decidedHope = hope?.origin === 'DECIDED' ? hope : null
  const [open, setOpen] = useState(false)
  const at = (m: number) => ({ day: clock.dateKey(m), time: clock.hm(m) })
  const initial = () => ({
    zone_id: task.zone_id,
    duration: String(task.duration),
    earliest_start: at(task.earliest_start),
    latest_start: at(task.latest_start),
    latest_end: at(task.latest_end),
  })
  const [f, setF] = useState(initial)
  // 자원 바꾸기: 고른 자원과 서버 확인
  const current = planned ? planned.resourceId : task.requested_resource_id
  const [pick, setPick] = useState(current ?? '')
  const [swap, setSwap] = useState<{ resourceId: string; check: ResourceCheck | null; error: string | null } | null>(
    null,
  )
  const askSwap = () => {
    const resourceId = pick
    setSwap({ resourceId, check: null, error: null })
    fetchResourceCheck(actorId, task.task_id, resourceId)
      .then((check) => setSwap((cur) => (cur?.resourceId === resourceId ? { ...cur, check } : cur)))
      .catch((err: unknown) =>
        setSwap((cur) =>
          cur?.resourceId === resourceId ? { ...cur, error: err instanceof Error ? err.message : String(err) } : cur,
        ),
      )
  }
  const confirmSwap = () => {
    if (!swap?.check) return
    const { resourceId, check } = swap
    setSwap(null)
    if (check.path === 'EDIT') {
      const resource = state.resources.find((r) => r.resource_id === resourceId)
      const body: Record<string, unknown> = { requested_resource_id: resourceId }
      if (resource && resource.resource_type !== task.required_resource_type) {
        body.required_resource_type = resource.resource_type
      }
      void run('요청 자원 고치기', `/tasks/${task.task_id}/edit`, body)
    } else void run('자원 바꾸기', `/tasks/${task.task_id}/resource`, { resource_id: resourceId })
  }
  const reasonText = (code: string) => ruleName(meta, code) ?? REASON[code] ?? code
  const text = (name: string): string => {
    if (name === 'work_type') return workTypeName(meta, task.work_type)
    if (name === 'zone_id') return `${task.zone_id} 구역`
    if (name === 'duration') return `${task.duration}분`
    if (name === 'required_resource_type') return task.required_resource_type ?? '없음'
    if (name === 'requested_resource_id') return task.requested_resource_id ?? '없음'
    return clock.format(task[name as TimeKey])
  }
  const days = (value: string) => {
    const opts = clock.workDays.map((d) => ({ key: d.key, label: d.label }))
    return opts.some((o) => o.key === value) ? opts : [...opts, { key: value, label: value }]
  }
  const off = busy !== null
  const denied = owner ? undefined : '담당자만 값을 고치거나 확인할 수 있습니다'

  // 바뀐 값만 보낸다. 시각 형식이 틀리면 보내지 않는다(형식 안내만 하고 판정은 서버가 한다)
  const save = () => {
    const body: Record<string, unknown> = {}
    if (f.zone_id !== task.zone_id) body.zone_id = f.zone_id
    const duration = Number(f.duration)
    if (Number.isFinite(duration) && duration > 0 && duration !== task.duration) body.duration = duration
    for (const key of TIME_KEYS) {
      const m = clock.toMinute(f[key].day, f[key].time)
      if (m !== null && m !== task[key]) body[key] = m
    }
    if (Object.keys(body).length === 0) return
    setOpen(false)
    void run('작업 값 고치기', `/tasks/${task.task_id}/edit`, body)
  }
  const badTime = TIME_KEYS.some((key) => clock.toMinute(f[key].day, f[key].time) === null)

  return (
    <div className="tl-edit">
      {(decided.length > 0 || decidedHope) && (
        <div className="tl-decided">
          <div className="block-label">Agent가 정한 값 (요청 문장에 없거나 해석·추정한 값)</div>
          {decided.map((name) => (
            <div key={name} className="small">
              <span className="tag tag-warn">정함</span> {VALUE_NAME[name] ?? name}: {text(name)}
            </div>
          ))}
          {decidedHope && (
            <div className="small">
              <span className="tag tag-warn">정함</span> 희망 영역: {clock.span(decidedHope.start, decidedHope.end)}
              <span className="muted"> · 확인하기 전에는 이 범위 안으로 옮기는 안도 담당자에게 묻습니다</span>
            </div>
          )}
        </div>
      )}
      <div className="tl-card-actions">
        {(decided.length > 0 || decidedHope) && (
          <button
            className="btn-small"
            disabled={off || !owner}
            title={
              denied ??
              '정한 값을 그대로 받아들입니다. 요청 자원에 동의한 것으로, 희망 영역은 말한 희망으로 기록됩니다'
            }
            onClick={() => void run('정한 값 확인', `/tasks/${task.task_id}/edit`, { confirm: true })}
          >
            정한 값 확인
          </button>
        )}
        <button
          className="btn-small"
          disabled={off || !owner}
          title={denied}
          onClick={() => {
            setF(initial())
            setOpen(!open)
          }}
        >
          {open ? '고치기 취소' : '값 고치기'}
        </button>
      </div>
      {open && (
        <div className="tl-edit-form small">
          <label>
            구역
            <select value={f.zone_id} onChange={(e) => setF({ ...f, zone_id: e.target.value })}>
              {state.zones.map((z) => (
                <option key={z} value={z}>
                  {z}
                </option>
              ))}
            </select>
          </label>
          <label>
            작업 시간(분)
            <input
              className="time"
              inputMode="numeric"
              value={f.duration}
              onChange={(e) => setF({ ...f, duration: e.target.value })}
            />
          </label>
          {TIME_KEYS.map((key) => (
            <label key={key}>
              {VALUE_NAME[key]}
              <span className="row tight">
                <select value={f[key].day} onChange={(e) => setF({ ...f, [key]: { ...f[key], day: e.target.value } })}>
                  {days(f[key].day).map((o) => (
                    <option key={o.key} value={o.key}>
                      {o.label}
                    </option>
                  ))}
                </select>
                <input
                  className="time"
                  placeholder="HH:MM"
                  value={f[key].time}
                  onChange={(e) => setF({ ...f, [key]: { ...f[key], time: e.target.value } })}
                />
              </span>
            </label>
          ))}
          {badTime && <p className="small bad tl-card-note">시각은 HH:MM 형식으로 적습니다.</p>}
          <p className="small muted tl-card-note">
            시각 셋은 가능 범위(반드시 지켜야 하는 범위)입니다. 바라는 시각은 타임라인에서 희망 영역으로 그립니다.
            고친 값은 사람이 말한 값이 됩니다. 검증은 요청 폼과 같고, 검토 중인 안은 무효가 됩니다. 지금 배치와 어긋나면 Agent가 다시 풉니다.
          </p>
          <div className="tl-card-actions">
            <button className="btn-small" disabled={off || badTime} onClick={save}>
              저장
            </button>
          </div>
        </div>
      )}
      {task.required_resource_type && (
        <div className="tl-edit-form small">
          <label>
            자원 {planned ? '(지금 배정)' : '(요청)'}
            <span className="row tight">
              <select value={pick} onChange={(e) => setPick(e.target.value)}>
                {state.resources.map((r) => (
                  <option key={r.resource_id} value={r.resource_id}>
                    {r.resource_id}
                  </option>
                ))}
              </select>
              <button
                className="btn-small"
                disabled={off || !owner || !pick || pick === current}
                title={denied ?? (pick === current ? '지금 자원과 같습니다' : undefined)}
                onClick={askSwap}
              >
                자원 바꾸기
              </button>
            </span>
          </label>
          {swap && (
            <div className="tl-decided">
              <div className="block-label">
                자원 {current ?? '없음'} → {swap.resourceId}
              </div>
              {!swap.check && !swap.error && <p className="small muted tl-card-note">서버가 확인하는 중…</p>}
              {swap.error && <p className="small tl-card-note">확인하지 못했습니다: {swap.error}</p>}
              {swap.check && !swap.check.ok && (
                <p className="small tl-card-note">
                  바꿀 수 없습니다: {swap.check.reason_codes.map(reasonText).join(', ')}
                </p>
              )}
              {swap.check?.ok && (
                <>
                  <p className="small tl-card-note">
                    {swap.check.path === 'EDIT'
                      ? '계획에 없는 요청입니다. 요청 자원을 고칩니다.'
                      : '지금 시각 그대로 자원만 바뀝니다. 자기 작업만 바뀌므로 Supervisor 승인 없이 확정됩니다.'}
                  </p>
                  {swap.check.invalidates.length > 0 && (
                    <p className="small tl-card-note">
                      확정하면 검토 중인 안 {swap.check.invalidates.length}개가 무효가 됩니다:{' '}
                      {swap.check.invalidates.map((id) => id.slice(0, 13)).join(', ')}
                    </p>
                  )}
                </>
              )}
              <div className="tl-card-actions">
                <button className="btn-small" disabled={!swap.check?.ok || busy !== null} onClick={confirmSwap}>
                  확정
                </button>
                <button className="btn-small" onClick={() => setSwap(null)}>
                  취소
                </button>
              </div>
            </div>
          )}
        </div>
      )}
      {denied && <p className="small muted tl-card-note">{denied}</p>}
    </div>
  )
}
