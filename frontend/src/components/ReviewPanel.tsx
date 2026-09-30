// 검토 패널 (§13, 부록 A.19). 서버 계산 결과만 보여 준다(모델 문장 없음, §11.6).
// 승인 버튼은 권한만 보고 켠다. STALE·Hold여도 막지 않고 서버의 거절 사유를 보여 준다.

import { useState } from 'react'
import type { CandidateView, CommandOutcome, SiteState } from '../types'
import {
  CANDIDATE_KIND,
  CANDIDATE_STATUS,
  CHECK_NAME,
  CHECK_STATUS,
  CONSULTATION_STATUS,
  ITEM_STATUS,
  REJECT_REASON,
  SCOPE_LEVEL,
  SOLVER_STATUS,
  VALIDATION_BADGE,
} from '../labels'
import { span } from '../time'
import { Code, OutcomeBox, ValidationBadge } from './common'

export type Run = (label: string, path: string, body: unknown) => Promise<void>

interface Props {
  state: SiteState
  candidate: CandidateView | null
  missing: boolean
  selectedId: string | null
  onSelect: (id: string) => void
  queueNotice: string | null
  isSupervisor: boolean
  busy: string | null
  run: Run
  outcome: CommandOutcome | null
}

const short = (id: string) => id.replace(/^cand_/, '').slice(0, 8)

export function ReviewPanel(props: Props) {
  const { state, candidate, missing, selectedId, onSelect, queueNotice, outcome } = props
  return (
    <section className="panel review">
      <div className="panel-head">
        <h2>검토 패널</h2>
        <span className="muted">서버 계산 결과</span>
      </div>
      <div className="panel-body">
        <div className="chips">
          {state.candidates.map((c) => (
            <button
              key={c.candidate_id}
              className={`chip ${c.candidate_id === selectedId ? 'chip-on' : ''} chip-${c.display_status.toLowerCase()}`}
              onClick={() => onSelect(c.candidate_id)}
              title={c.candidate_id}
            >
              {short(c.candidate_id)} · {CANDIDATE_KIND[c.kind]} ·{' '}
              {c.display_status === 'COMMITTED'
                ? '확정됨'
                : state.review_queue.includes(c.candidate_id)
                  ? '검토 대기'
                  : (CANDIDATE_STATUS[c.display_status] ?? c.display_status)}
            </button>
          ))}
          {state.candidates.length === 0 && <span className="muted">후보 없음</span>}
        </div>
        {queueNotice && (
          <div className="notice">
            새 검토 대기 후보 {short(queueNotice)}{' '}
            <button onClick={() => onSelect(queueNotice)}>보기</button>
          </div>
        )}
        {candidate ? (
          <CandidateDetail
            key={`${candidate.candidate_id}:${candidate.consultation ? 'c' : '-'}`} {...props} candidate={candidate} missing={missing} />
        ) : (
          <p className="muted">
            검토 대기 후보가 없습니다. 위 목록에서 후보를 고르면 내용을 봅니다. 현재 충돌{' '}
            {state.conflicts.length}건.
          </p>
        )}
        <h3>마지막 명령 결과</h3>
        {outcome ? <OutcomeBox outcome={outcome} /> : <p className="muted">없음</p>}
      </div>
    </section>
  )
}

function CandidateDetail({
  state,
  candidate: c,
  missing,
  isSupervisor,
  busy,
  run,
}: Props & { candidate: CandidateView }) {
  const origin = state.site.horizon_start_utc
  const actorName = new Map(state.actors.map((a) => [a.actor_id, a.name]))
  const v = c.validation
  const items = c.consultation?.items ?? []
  const [waiveIds, setWaiveIds] = useState<string[]>(
    items.filter((i) => i.item_status === 'PENDING').map((i) => i.task_id),
  )
  const [waiveComment, setWaiveComment] = useState('')
  const [rejReason, setRejReason] = useState('')
  const [rejTargets, setRejTargets] = useState<string[]>([])
  const [rejAxes, setRejAxes] = useState<string[]>([])
  const [rejComment, setRejComment] = useState('')
  const needSup = isSupervisor ? undefined : 'Supervisor 권한 필요'
  const assign = (a: { start: number; end: number; resource_id: string | null }) =>
    `${span(origin, a.start, a.end)}${a.resource_id ? ` · ${a.resource_id}` : ''}`
  const toggle = (list: string[], x: string) =>
    list.includes(x) ? list.filter((y) => y !== x) : [...list, x]

  return (
    <div className="cand">
      <div className="cand-head">
        <code title={c.candidate_id}>{c.candidate_id}</code>
        <span className={`badge badge-cand-${c.display_status.toLowerCase()}`}>
          후보: {CANDIDATE_STATUS[c.display_status] ?? c.display_status}
        </span>
        {v ? <ValidationBadge status={v.display_status} /> : <span className="badge">검증 대기</span>}
        {missing && <span className="badge badge-stale">목록에서 제외됨(갱신 중단)</span>}
      </div>
      {v && v.display_status === 'STALE' && (
        <p className="small muted">검사 당시 결과: {VALIDATION_BADGE[v.status]?.text ?? v.status}</p>
      )}
      <p className="small muted">
        {CANDIDATE_KIND[c.kind]} · Context v{c.context_version} · 기준 Plan R{c.base_plan_revision}
        {c.run_id && ` · Run ${c.run_id}`}
      </p>

      <h3>변경점</h3>
      {c.changes.length === 0 ? (
        <p className="muted">변경 없음</p>
      ) : (
        <table className="tbl">
          <tbody>
            {c.changes.map((ch) => (
              <tr key={ch.task_id}>
                <th>{ch.task_id}</th>
                <td>{assign(ch.before)}</td>
                <td>→</td>
                <td className="strong">{assign(ch.after)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {c.solver && (
        <>
          <h3>Solver</h3>
          <table className="tbl">
            <tbody>
              <tr>
                <th>탐색 범위</th>
                <td>{SCOPE_LEVEL[c.solver.scope_level] ?? c.solver.scope_level}</td>
              </tr>
              <tr>
                <th>1단계 (변경 수)</th>
                <td>
                  {SOLVER_STATUS[c.solver.stage1.status] ?? c.solver.stage1.status} · 변경{' '}
                  {c.solver.stage1.changed ?? '—'} <code>{c.solver.stage1.status}</code>
                </td>
              </tr>
              <tr>
                <th>2단계 (지연)</th>
                <td>
                  {c.solver.stage2 ? (
                    <>
                      {SOLVER_STATUS[c.solver.stage2.status] ?? c.solver.stage2.status} · 지연{' '}
                      {c.solver.stage2.delay ?? '—'}분 <code>{c.solver.stage2.status}</code>
                    </>
                  ) : (
                    '실행 안 함'
                  )}
                </td>
              </tr>
            </tbody>
          </table>
          <p className="small">
            {c.solver.minimal_change && <span className="tag">최소 변경(이 탐색 범위 안)</span>}
            {c.solver.delay_optimality_unconfirmed && <span className="tag tag-warn">지연 최적성 미확정</span>}
          </p>
        </>
      )}

      <h3>Validation</h3>
      {v ? (
        <table className="tbl checks">
          <tbody>
            {v.checks.map((ck, i) => (
              <tr key={`${ck.check_id}:${i}`} className={`ck-${ck.status.toLowerCase()}`}>
                <th>
                  {ck.check_id} {CHECK_NAME[ck.check_id] ?? ''}
                </th>
                <td>{CHECK_STATUS[ck.status] ?? ck.status}</td>
                <td>
                  {ck.task_ids.join(', ')} {ck.reason_code && <Code code={ck.reason_code} />}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">아직 검증 기록 없음</p>
      )}

      <h3>
        협의{' '}
        {c.consultation && (
          <span className="muted">
            {CONSULTATION_STATUS[c.consultation.status] ?? c.consultation.status}
          </span>
        )}
      </h3>
      {c.consultation ? (
        <>
          <table className="tbl">
            <tbody>
              {items.map((it) => (
                <tr key={it.task_id} className={`item-${it.item_status.toLowerCase()}`}>
                  <td>
                    <input
                      type="checkbox"
                      checked={waiveIds.includes(it.task_id)}
                      onChange={() => setWaiveIds(toggle(waiveIds, it.task_id))}
                      aria-label={`${it.task_id} 수용 대상`}
                    />
                  </td>
                  <th>{it.task_id}</th>
                  <td>{actorName.get(it.owner_actor_id) ?? it.owner_actor_id}</td>
                  <td className="small">
                    {assign(it.before)} → {assign(it.after)}
                  </td>
                  <td>{ITEM_STATUS[it.item_status] ?? it.item_status}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {items.length === 0 && <p className="muted">협의 항목 없음</p>}
          <div className="row">
            <input
              className="grow"
              placeholder="수용 사유"
              value={waiveComment}
              onChange={(e) => setWaiveComment(e.target.value)}
            />
            <button
              disabled={!isSupervisor || busy !== null}
              title={needSup}
              onClick={() =>
                run('협의 항목 수용(WAIVE)', `/consultations/${c.candidate_id}/waive`, {
                  task_ids: waiveIds,
                  comment: waiveComment,
                })
              }
            >
              선택 항목 수용
            </button>
          </div>
        </>
      ) : (
        <p className="muted">협의 정보 없음</p>
      )}

      <div className="approve-row">
        <button
          className="btn-primary"
          disabled={!isSupervisor || busy !== null}
          title={needSup}
          onClick={() =>
            run('후보 승인', `/candidates/${c.candidate_id}/approve`, {
              validation_id: v?.validation_id ?? '',
              expected_context_version: c.context_version,
            })
          }
        >
          승인·확정
        </button>
        {!isSupervisor && <span className="muted small">Supervisor 권한 필요</span>}
      </div>

      <details className="reject">
        <summary>거절</summary>
        <div className="form-grid">
          <label>
            사유 코드
            <select value={rejReason} onChange={(e) => setRejReason(e.target.value)}>
              <option value="">선택</option>
              {Object.entries(REJECT_REASON).map(([k, t]) => (
                <option key={k} value={k}>
                  {t} ({k})
                </option>
              ))}
            </select>
          </label>
          <fieldset>
            <legend>대상 작업</legend>
            {state.tasks.map((t) => (
              <label key={t.task_id} className="inline">
                <input
                  type="checkbox"
                  checked={rejTargets.includes(t.task_id)}
                  onChange={() => setRejTargets(toggle(rejTargets, t.task_id))}
                />
                {t.task_id}
              </label>
            ))}
          </fieldset>
          <fieldset>
            <legend>고정할 축</legend>
            {['TIME', 'RESOURCE'].map((a) => (
              <label key={a} className="inline">
                <input
                  type="checkbox"
                  checked={rejAxes.includes(a)}
                  onChange={() => setRejAxes(toggle(rejAxes, a))}
                />
                {a === 'TIME' ? '시간' : '자원'} ({a})
              </label>
            ))}
          </fieldset>
          <label>
            사유
            <input value={rejComment} onChange={(e) => setRejComment(e.target.value)} />
          </label>
        </div>
        <div className="row">
          <button
            type="button"
            onClick={() => {
              setRejReason('TASK_IMMOVABLE')
              setRejTargets(['C'])
              setRejAxes(['TIME', 'RESOURCE'])
              setRejComment('작업발판 연계 공정 확정')
            }}
          >
            시연값 채우기
          </button>
          <button
            className="btn-warn"
            disabled={!isSupervisor || busy !== null}
            title={needSup}
            onClick={() =>
              run('후보 거절', `/candidates/${c.candidate_id}/reject`, {
                validation_id: v?.validation_id ?? '',
                reason_code: rejReason,
                target_task_ids: rejTargets,
                axes: rejAxes,
                comment: rejComment,
              })
            }
          >
            거절
          </button>
        </div>
      </details>
    </div>
  )
}
