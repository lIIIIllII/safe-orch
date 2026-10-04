// 작업 카드의 "Agent가 정한 값"과 값 고치기·확인(AG-33). 담당자(요청자)만 한다.
// Work Intake가 정한 값(문장에 없거나 해석·추정이 들어간 값)을 따로 보여 주고, 담당자가 고치거나 그대로 확인한다.
// 고친 값과 확인한 값은 사람이 말한 값이 된다. 검증은 서버가 폼과 같은 규칙으로 한다(화면은 판정하지 않는다).

import { useState } from 'react'
import { decidedValues, useEnv, workTypeName } from '../context'
import { VALUE_NAME } from '../labels'
import type { SiteState, Task } from '../types'
import type { Run } from './ReviewPanel'

type TimeKey = 'earliest_start' | 'latest_start' | 'latest_end'
const TIME_KEYS: TimeKey[] = ['earliest_start', 'latest_start', 'latest_end']

interface Props {
  task: Task
  state: SiteState
  /** 지금 Actor가 이 작업의 담당자인가(권한 안내용, 판정은 서버) */
  owner: boolean
  busy: string | null
  run: Run
}

export function TaskEdit({ task, state, owner, busy, run }: Props) {
  const { clock, meta } = useEnv()
  const decided = decidedValues(task)
  const [open, setOpen] = useState(false)
  const at = (m: number) => ({ day: clock.dateKey(m), time: clock.hm(m) })
  const initial = () => ({
    zone_id: task.zone_id,
    duration: String(task.duration),
    requested_resource_id: task.requested_resource_id ?? '',
    earliest_start: at(task.earliest_start),
    latest_start: at(task.latest_start),
    latest_end: at(task.latest_end),
  })
  const [f, setF] = useState(initial)
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
    if (f.requested_resource_id && f.requested_resource_id !== task.requested_resource_id) {
      const resource = state.resources.find((r) => r.resource_id === f.requested_resource_id)
      body.requested_resource_id = f.requested_resource_id
      if (resource && resource.resource_type !== task.required_resource_type) {
        body.required_resource_type = resource.resource_type
      }
    }
    if (Object.keys(body).length === 0) return
    setOpen(false)
    void run('작업 값 고치기', `/tasks/${task.task_id}/edit`, body)
  }
  const badTime = TIME_KEYS.some((key) => clock.toMinute(f[key].day, f[key].time) === null)

  return (
    <div className="tl-edit">
      {decided.length > 0 && (
        <div className="tl-decided">
          <div className="block-label">Agent가 정한 값 (요청 문장에 없거나 해석·추정한 값)</div>
          {decided.map((name) => (
            <div key={name} className="small">
              <span className="tag tag-warn">정함</span> {VALUE_NAME[name] ?? name}: {text(name)}
            </div>
          ))}
        </div>
      )}
      <div className="tl-card-actions">
        {decided.length > 0 && (
          <button
            className="btn-small"
            disabled={off || !owner}
            title={denied ?? '정한 값을 그대로 받아들입니다. 시작 범위·요청 자원에 동의한 것으로 기록됩니다'}
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
          {task.required_resource_type && (
            <label>
              요청 자원
              <select
                value={f.requested_resource_id}
                onChange={(e) => setF({ ...f, requested_resource_id: e.target.value })}
              >
                {state.resources.map((r) => (
                  <option key={r.resource_id} value={r.resource_id}>
                    {r.resource_id}
                  </option>
                ))}
              </select>
            </label>
          )}
          {badTime && <p className="small bad tl-card-note">시각은 HH:MM 형식으로 적습니다.</p>}
          <p className="small muted tl-card-note">
            고친 값은 사람이 말한 값이 됩니다. 검증은 요청 폼과 같고, 검토 중인 안은 무효가 됩니다. 지금 배치와 어긋나면 Agent가 다시 풉니다.
          </p>
          <div className="tl-card-actions">
            <button className="btn-small" disabled={off || badTime} onClick={save}>
              저장
            </button>
          </div>
        </div>
      )}
      {denied && <p className="small muted tl-card-note">{denied}</p>}
    </div>
  )
}
