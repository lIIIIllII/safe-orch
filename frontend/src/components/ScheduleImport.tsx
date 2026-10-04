// 일정 넣기: 파일(JSON) → 서버 미리보기(작업별 판정) → 뺄 작업 고르기 → 넣기.
// 화면은 판정하지 않는다. 판정과 사유는 서버가 준 것을 그대로 보이고, [넣기]는 판정으로 막지 않는다 (UI-01).

import { useEffect, useState } from 'react'
import { fetchSchedulePreview } from '../api'
import { useEnv } from '../context'
import { IMPORT_VERDICT } from '../labels'
import type { SchedulePreview, SiteState } from '../types'
import { Code } from './common'
import type { Run } from './ReviewPanel'

interface Props {
  state: SiteState
  actorId: string
  roles: string[]
  busy: string | null
  run: Run
}

export function ScheduleImport({ actorId, roles, busy, run }: Props) {
  const { siteId } = useEnv()
  const [name, setName] = useState('')
  const [doc, setDoc] = useState<unknown>(null)
  const [exclude, setExclude] = useState<string[] | null>(null) // null이면 서버 판정대로 처음 한 번 채운다
  const [preview, setPreview] = useState<SchedulePreview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const allowed = roles.includes('UNIT_PLANNER')

  // 문서·뺄 작업·Actor가 바뀔 때마다 미리보기를 다시 받는다
  useEffect(() => {
    if (doc === null) return
    let cancelled = false
    fetchSchedulePreview(siteId, actorId, doc, exclude ?? [])
      .then((p) => {
        if (cancelled) return
        setPreview(p)
        setError(null)
        // 서버가 넣을 수 없다고 판정한 작업은 처음부터 빼기로 표시한다(서버 판정을 보여 주는 것)
        if (exclude === null) {
          setExclude(p.tasks.filter((t) => t.verdict === 'REJECTED').map((t) => t.task_id))
        }
      })
      .catch((e) => {
        if (!cancelled) setError(String(e))
      })
    return () => {
      cancelled = true
    }
  }, [siteId, actorId, doc, exclude])

  const load = async (file: File | undefined) => {
    if (!file) return
    setName(file.name)
    setPreview(null)
    setExclude(null)
    try {
      setDoc(JSON.parse(await file.text()))
      setError(null)
    } catch {
      setDoc(null)
      setError('JSON으로 읽을 수 없는 파일입니다')
    }
  }

  const toggle = (taskId: string) =>
    setExclude((cur) => {
      const list = cur ?? []
      return list.includes(taskId) ? list.filter((t) => t !== taskId) : [...list, taskId]
    })

  const submit = async () => {
    const response = await run('일정 넣기', `/sites/${siteId}/schedules/import`, { document: doc, exclude: exclude ?? [] })
    if (response?.status === 'APPLIED') setExclude((cur) => [...(cur ?? [])]) // 넣은 뒤의 판정을 다시 받는다
  }

  return (
    <div className="form-grid">
      <p className="muted small span2">
        꺼낸 일정 문서(JSON)를 고쳐서 넣습니다. 자기 Unit의 작업만 넣을 수 있고, 문서의 시각은 희망 영역이 됩니다(가능
        범위는 계획 기간 전체). 반드시 지켜야 하는 범위는 넣은 뒤 작업 카드에서 고칩니다.
      </p>
      <label className="span2">
        일정 문서
        <input type="file" accept="application/json,.json" onChange={(e) => void load(e.target.files?.[0])} />
      </label>
      {error && <p className="bad small span2">{error}</p>}
      {preview && (
        <div className="span2">
          <p className="small">
            <b>{name}</b> <span className="muted">서버 판정</span>:{' '}
            {preview.acceptable ? '이대로 넣을 수 있습니다' : '넣을 수 없는 작업이 있습니다'}
            {preview.pack_mismatch && <span className="warn"> · 문서를 꺼낸 Pack이 지금과 다릅니다(값은 지금 Pack으로 검증)</span>}
          </p>
          {preview.reasons.map((code) => (
            <p key={code} className="bad small">
              <Code code={code} />
            </p>
          ))}
          {preview.details.map((d) => (
            <p key={d} className="muted small">
              {d}
            </p>
          ))}
          {preview.tasks.length > 0 && (
            <table className="tbl small">
              <thead>
                <tr>
                  <th>빼기</th>
                  <th>작업</th>
                  <th>판정</th>
                  <th>사유·바뀌는 것</th>
                </tr>
              </thead>
              <tbody>
                {preview.tasks.map((t) => (
                  <tr key={t.task_id} className={t.excluded ? 'muted' : undefined}>
                    <td>
                      <input type="checkbox" checked={t.excluded} onChange={() => toggle(t.task_id)} />
                    </td>
                    <td>{t.task_id}</td>
                    <td>{IMPORT_VERDICT[t.verdict] ?? t.verdict}</td>
                    <td>
                      {t.reasons.map((code) => (
                        <Code key={code} code={code} />
                      ))}
                      {t.changed.length > 0 && <span> 값: {t.changed.join(', ')}</span>}
                      {t.hope_changed && <span> 희망 영역</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      <div className="row span2">
        <span className="spacer" />
        <button
          className="btn-primary"
          disabled={!allowed || busy !== null || doc === null}
          title={allowed ? undefined : '공정 담당(UNIT_PLANNER) 권한 필요'}
          onClick={() => void submit()}
        >
          넣기
        </button>
      </div>
      {!allowed && <p className="muted small span2">공정 담당(UNIT_PLANNER) 권한 필요</p>}
    </div>
  )
}
