// 검토 패널. 서버 계산 결과만 보여 준다(모델 문장 없음).
// 승인 버튼은 권한만 보고 켠다. STALE·Hold여도 막지 않고 서버의 거절 사유를 보여 준다.

import { Fragment, useState } from 'react'
import type { CandidateView, CommandOutcome, CommandResponse, FactChange, SiteState } from '../types'
import {
  APPROACH,
  CANDIDATE_KIND,
  CONTESTED_BY,
  CANDIDATE_STATUS,
  CHECK_NAME,
  CHECK_STATUS,
  CONSULTATION_STATUS,
  ITEM_STATUS,
  REJECT_REASON,
  OBJECTIVE,
  SCOPE_LEVEL,
  SOLVER_STATUS,
  VALIDATION_BADGE,
  VALUE_NAME,
} from '../labels'
import { delayText } from '../time'
import { decidedValues, useEnv } from '../context'
import { Code, OutcomeBox, ValidationBadge } from './common'

/** 명령 실행. 응답(실패 시 null)을 돌려준다. 결과 영역 표시는 App이 한다. */
export type Run = (label: string, path: string, body: unknown) => Promise<CommandResponse | null>

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
        <PlanCompare {...props} />
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

/** 사실 변경 표시: 기준 계획을 확정한 뒤 사람이 바꾼 사실. 차이는 서버가 계산하고 화면은 풀어 쓰기만 한다. */
function FactChanges({ changes, base, state }: { changes: FactChange[]; base: number; state: SiteState }) {
  const { clock } = useEnv()
  const actorName = new Map(state.actors.map((a) => [a.actor_id, a.name]))
  if (changes.length === 0) return null
  const TIMES = ['earliest_start', 'latest_start', 'latest_end']
  const value = (field: string, v: number | string | null) =>
    v === null
      ? '없음'
      : typeof v === 'number'
        ? TIMES.includes(field)
          ? clock.format(v)
          : `${v}분`
        : String(v)
  const line = (x: FactChange): string => {
    switch (x.kind) {
      case 'TASK_ADDED':
        return `${x.task_id} 새로 들어온 작업`
      case 'TASK_REMOVED':
        return `${x.task_id} 없앤 작업(없애기·철회)`
      case 'PINNED':
        return `${x.task_id} 고정 · ${actorName.get(x.pinned_by) ?? x.pinned_by}${x.by_role === 'SUPERVISOR' ? ' (Supervisor)' : ''}`
      case 'UNPINNED':
        return `${x.task_id} 고정 해제`
      case 'VALUE_CHANGED':
        return `${x.task_id} ${VALUE_NAME[x.field] ?? x.field}: ${value(x.field, x.before)} → ${value(x.field, x.after)}`
    }
  }
  return (
    <div className="fact-changes">
      <b>기준 계획 R{base} 확정 뒤 바뀐 사실</b> <span className="muted small">서버 계산</span>
      <ul>
        {changes.map((x, i) => (
          <li key={`${x.kind}:${x.task_id}:${i}`}>{line(x)}</li>
        ))}
      </ul>
    </div>
  )
}

type PlanChangeView = CandidateView['plan_changes'][number]

/** 기준에서 옮긴 방향과 거리: "30분 늦음", "1시간 앞당김". 방향과 거리는 서버 계산이다. */
const movedText = (x: PlanChangeView) =>
  x.direction === null ? '' : `${delayText(x.delay, x.work_delay)} ${x.direction === 'EARLY' ? '앞당김' : '늦음'}`

/** 그 안에서 그 작업을 기준에서 옮긴 방향과 거리. 옮기지 않았으면 빈 문자열 */
const movedOf = (c: CandidateView, taskId: string) => {
  const x = c.plan_changes.find((p) => p.task_id === taskId)
  return x ? movedText(x) : ''
}

/** 안 번호: 서버가 매긴다(고정). 재계획 호출마다 번호 하나, 한 호출의 여러 안은 가·나, 다른 호출이 같은
 *  배치를 냈으면 번호를 합친다("1가·2안"). 안 비교에는 살아 있는 안만 나오므로 번호가 건너뛸 수 있다. */
function planLabel(c: CandidateView): string {
  return c.plan_label === null ? short(c.candidate_id) : `${c.plan_label}안`
}

/** 그 후보의 배치에 도달한 접근 이름(겹치지 않게). 둘 이상이면 다른 접근이 같은 배치를 낸 것이다. */
const approachNames = (c: CandidateView) => [...new Set(c.approaches.map((a) => APPROACH[a.approach] ?? a.approach))]

/** 안이 바꾸는 것 한 작업분: 시각 줄과 자원 줄. 목록·방향·옮긴 거리·"요청 자원과 다름"은 서버 계산이고 화면은
 *  풀어 쓰기만 한다. 새 작업은 기준 위치(요청한 자리·시작 범위)가 있으면 그것과 견줘 적고, 없으면 "새 배치"다. */
function PlanChange({ x }: { x: PlanChangeView }) {
  const { clock, meta } = useEnv()
  const type = x.resource_type ? (meta.resource_types[x.resource_type] ?? x.resource_type) : ''
  const off = x.off_request ? '요청 자원과 다름' : ''
  const moved = movedText(x)
  if (x.kind === 'NEW') {
    const base = x.base
    const from = !base
      ? '기준 없음'
      : base.start === base.start_max
        ? `기준 ${clock.format(base.start)}`
        : `기준 ${clock.format(base.start)} ~ ${clock.format(base.start_max)}`
    return (
      <div className={x.resource_changed ? 'chg chg-resource' : 'chg'}>
        {x.resource_changed && <span className="tag tag-resource">자원</span>}
        {x.task_id} 새 배치: {clock.format(x.after.start)}
        {x.after.resource_id && ` · ${x.after.resource_id}`}
        {off && `(${off})`}
        <span className="muted">
          {' '}
          · {from}
          {base?.origin === 'DECIDED' && '(Agent가 정함)'}
          {base && (moved ? ` → ${moved}` : ' 안')}
        </span>
      </div>
    )
  }
  return (
    <>
      {x.time_changed && x.before && (
        <div className="chg">
          {x.task_id} {clock.format(x.before.start)} → {clock.format(x.after.start)}
          {moved && <span className="muted"> · {moved}</span>}
        </div>
      )}
      {x.resource_changed && (
        <div className="chg chg-resource">
          <span className="tag tag-resource">자원</span>
          {x.task_id} {type} {x.before?.resource_id ?? '없음'} → {x.after.resource_id ?? '없음'}
          {off && ` · ${off}`}
        </div>
      )}
    </>
  )
}

/** 이 Case의 안을 나란히: 접근, 바뀌는 것, 서버 지표, 필요한 확인, 조건, 거절·이견된 변경, 고르기.
 *  지표·확인·표시는 서버 계산이고, 접근의 이유 문장은 Agent가 쓴 것이다(UI-06).
 *  좁은 폭에서도 깨지지 않게: 바뀌는 것 칸만 줄바꿈하고, Agent 이유는 그 안 줄 아래에 표 전체 폭으로 둔다
 *  (두 줄까지 보이고 누르면 펼친다). 패널 폭이 모자라면 표 영역만 가로로 스크롤된다. */
function PlanCompare({ state, candidate, selectedId, onSelect, isSupervisor, busy, run }: Props) {
  // 이유를 펼친 안
  const [opened, setOpened] = useState<string[]>([])
  // [모두 거절]의 사유
  const [allReason, setAllReason] = useState('')
  const queue = state.candidates.filter((c) => state.review_queue.includes(c.candidate_id))
  const caseId = candidate?.case_id ?? queue[0]?.case_id ?? null
  const plans = queue.filter((c) => c.case_id === caseId)
  const actorName = new Map(state.actors.map((a) => [a.actor_id, a.name]))
  if (plans.length === 0) return null
  const denied = isSupervisor ? undefined : 'Supervisor만 안을 고를 수 있습니다'
  // 그 안이 담은(바꾸거나 새로 넣은) 작업 가운데 Agent가 정한 값이 남은 것
  const taskMap = new Map(state.tasks.map((t) => [t.task_id, t]))
  const decided = (c: CandidateView): [string, string[]][] =>
    c.changes.flatMap((x): [string, string[]][] => {
      const t = taskMap.get(x.task_id)
      const names = t ? decidedValues(t).map((n) => VALUE_NAME[n] ?? n) : []
      return names.length > 0 ? [[x.task_id, names]] : []
    })
  return (
    <div className="plans">
      <h3>
        안 비교 <span className="muted small">지표·필요한 확인은 서버 계산 · 이유는 Agent 문장</span>
      </h3>
      {/* 같은 Case의 안은 같은 사실 위에 있다: 사실 변경은 한 번만 보인다 */}
      <FactChanges changes={plans[0].fact_changes} base={plans[0].base_plan_revision} state={state} />
      {/* 모두 거절: 버튼은 역할만 보고 켠다. 사유가 비었거나 거절할 안이 없으면 서버가 거절한다(UI-01) */}
      <div className="row">
        <input
          className="grow"
          placeholder="모두 거절하는 사유(한 번만 적습니다)"
          value={allReason}
          onChange={(e) => setAllReason(e.target.value)}
        />
        <button
          className="btn-warn"
          disabled={!isSupervisor || busy !== null || caseId === null}
          title={denied ?? '이 Case의 살아 있는 안을 한 번에 거절합니다. 사유는 재계획에 한 번만 전해집니다'}
          onClick={async () => {
            const res = await run('모두 거절', `/cases/${caseId}/reject-all`, { comment: allReason })
            if (res?.status === 'APPLIED') setAllReason('')
          }}
        >
          모두 거절
        </button>
      </div>
      <div className="plans-scroll">
      <table className="tbl small plans-table">
        <thead>
          <tr>
            <th>안</th>
            <th>접근</th>
            <th>바뀌는 것</th>
            <th>지표</th>
            <th title="기준에서 바뀐 작업의 담당자 확인. 고른 안만 협의합니다">필요한 확인</th>
            <th>조건·표시</th>
            <th>고르기</th>
          </tr>
        </thead>
        <tbody>
          {plans.map((c) => {
            const items = c.consultation?.items ?? []
            const need = items.filter((i) => i.item_status === 'PENDING')
            const objected = items.filter((i) => i.item_status === 'OBJECTED')
            const delay = c.changes.reduce((n, x) => n + x.delay, 0)
            const workDelay = c.changes.reduce((n, x) => n + x.work_delay, 0)
            const reasons = c.approaches.filter((a) => a.quoted_reason)
            const open = opened.includes(c.candidate_id)
            const rowClass = `${c.candidate_id === selectedId ? 'plan-on' : ''} ${c.chosen ? 'plan-chosen' : ''}`
            return (
              <Fragment key={c.candidate_id}>
              <tr
                className={`plan-row ${reasons.length > 0 ? 'plan-has-reason' : ''} ${rowClass}`}
                onClick={() => onSelect(c.candidate_id)}
              >
                <td>
                  <b>{planLabel(c)}</b>
                  {c.chosen && <div className="tag">고른 안</div>}
                </td>
                <td>
                  {c.approaches.length === 0 && <span className="muted">—</span>}
                  {c.approaches.map((a) => (
                    <div key={`${a.run_id}`}>
                      {APPROACH[a.approach] ?? a.approach}
                      {a.same && <span className="muted"> (같은 배치에 도달)</span>}
                    </div>
                  ))}
                  {approachNames(c).length > 1 && <div className="tag">{approachNames(c).join('·')} 동일 의견</div>}
                </td>
                <td className="wrap">
                  {c.plan_changes.length === 0 && <span className="muted">없음</span>}
                  {c.plan_changes.map((x) => (
                    <PlanChange key={x.task_id} x={x} />
                  ))}
                </td>
                <td title={`기준에서 바뀐 작업 수 · 자원이 바뀌는 작업 수 · 기준에서 옮긴 거리 ${delayText(delay, workDelay)}`}>
                  변경 {c.changes.length} · 자원 {c.plan_changes.filter((x) => x.resource_changed).length} · 기준에서 옮긴 거리{' '}
                  {delay}분
                  {c.plan_changes
                    .filter((x) => x.direction !== null)
                    .map((x) => (
                      <div key={`o:${x.task_id}`} className="muted" title="기준 시작 범위에서 옮긴 방향과 거리">
                        {x.task_id} {movedText(x)}
                      </div>
                    ))}
                </td>
                <td>
                  {need.length === 0 && objected.length === 0 && <span className="muted">없음</span>}
                  {need.map((i) => (
                    <div key={i.task_id}>
                      {i.task_id} · {actorName.get(i.owner_actor_id) ?? i.owner_actor_id}
                    </div>
                  ))}
                  {objected.map((i) => (
                    <div key={i.task_id} className="bad">
                      {i.task_id} 이견 · {actorName.get(i.owner_actor_id) ?? i.owner_actor_id}
                    </div>
                  ))}
                  {c.chosen && c.consultation && (
                    <div className="muted">
                      협의 {CONSULTATION_STATUS[c.consultation.status] ?? c.consultation.status}
                    </div>
                  )}
                </td>
                <td>
                  {c.conditions.length > 0 && <div>조건 {c.conditions.map((x) => x.task_id).join(', ')}</div>}
                  {c.contested.map((x) => (
                    <span key={`${x.task_id}:${x.by}`} className="tag tag-warn">
                      {x.task_id} {CONTESTED_BY[x.by] ?? x.by}
                    </span>
                  ))}
                  {decided(c).map(([taskId, names]) => (
                    <div key={`d:${taskId}`} title="요청 문장에 없거나 해석·추정이 들어가 Agent가 정한 값입니다">
                      <span className="tag tag-warn">정함</span> {taskId}: {names.join(', ')}
                    </div>
                  ))}
                  {c.conditions.length === 0 && c.contested.length === 0 && decided(c).length === 0 && (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>
                  <button
                    className="btn-small"
                    disabled={!isSupervisor || busy !== null || c.chosen || !c.validation}
                    title={denied ?? (c.chosen ? '이미 고른 안입니다' : undefined)}
                    onClick={(e) => {
                      e.stopPropagation()
                      void run('안 고르기', `/candidates/${c.candidate_id}/choose`, {
                        validation_id: c.validation?.validation_id ?? '',
                      })
                    }}
                  >
                    {c.chosen ? '고름' : '이 안 고르기'}
                  </button>
                </td>
              </tr>
              {reasons.length > 0 && (
                <tr
                  className={`plan-reason ${rowClass}`}
                  title={open ? '누르면 접습니다' : '누르면 펼칩니다'}
                  onClick={() =>
                    setOpened(open ? opened.filter((id) => id !== c.candidate_id) : [...opened, c.candidate_id])
                  }
                >
                  <td colSpan={7}>
                    <div className={`agent-quote plan-reason-text ${open ? '' : 'plan-reason-clamp'}`}>
                      {reasons.map((a) => (
                        <span key={a.run_id}>
                          Agent({APPROACH[a.approach] ?? a.approach}): “{a.quoted_reason}”{' '}
                        </span>
                      ))}
                    </div>
                  </td>
                </tr>
              )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
      </div>
      {!isSupervisor && <p className="muted small">{denied}</p>}
      <p className="muted small">
        고른 안만 담당자 협의로 갑니다. 협의가 끝나면 아래에서 승인합니다. 고르지 않은 안은 그대로 남습니다.
      </p>
    </div>
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
  const { clock, scenario } = useEnv()
  const actorName = new Map(state.actors.map((a) => [a.actor_id, a.name]))
  const v = c.validation
  const items = c.consultation?.items ?? []
  const [waiveIds, setWaiveIds] = useState<string[]>(
    items.filter((i) => i.item_status === 'PENDING').map((i) => i.task_id),
  )
  const [waiveComment, setWaiveComment] = useState('')
  const [rejReason, setRejReason] = useState('')
  const [rejTargets, setRejTargets] = useState<string[]>([])
  const [rejComment, setRejComment] = useState('')
  const needSup = isSupervisor ? undefined : 'Supervisor 권한 필요'
  const assign = (a: { start: number; end: number; resource_id: string | null }) =>
    `${clock.span(a.start, a.end)}${a.resource_id ? ` · ${a.resource_id}` : ''}`
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
        {c.plan_label !== null && <span className="badge">{planLabel(c)}</span>}
        {c.chosen && <span className="badge">고른 안</span>}
        {missing && <span className="badge badge-stale">목록에서 제외됨(갱신 중단)</span>}
      </div>
      {v && v.display_status === 'STALE' && (
        <p className="small muted">검사 당시 결과: {VALIDATION_BADGE[v.status]?.text ?? v.status}</p>
      )}
      <p className="small muted">
        {CANDIDATE_KIND[c.kind]} · Context v{c.context_version} · 기준 Plan R{c.base_plan_revision}
        {c.run_id && ` · Run ${c.run_id}`}
      </p>
      {c.contested.length > 0 && (
        <div className="contested">
          <b>거절·이견된 변경 포함</b> <span className="muted small">서버 계산(같은 변경인지만 비교)</span>
          <div>
            {c.contested.map((x) => (
              <span key={`${x.task_id}:${x.by}`} className="tag tag-warn">
                {x.task_id} · {CONTESTED_BY[x.by] ?? x.by}
              </span>
            ))}
          </div>
        </div>
      )}
      {c.conditions.length > 0 && (
        <div className="conditions">
          <b>Agent가 건 조건</b> <span className="muted small">Agent가 고르고 서버가 유효성만 검사한 값</span>
          <ul>
            {c.conditions.map((x) => (
              <li key={x.task_id}>
                {x.task_id}
                {x.start_min !== null && x.start_min === x.start_max && ` 시작 = ${clock.format(x.start_min)}`}
                {x.start_min !== null && x.start_min !== x.start_max && ` 시작 ≥ ${clock.format(x.start_min)}`}
                {x.start_max !== null && x.start_min !== x.start_max && ` 시작 ≤ ${clock.format(x.start_max)}`}
                {x.resource_id && ` 자원 = ${x.resource_id}`}
              </li>
            ))}
          </ul>
        </div>
      )}

      {c.rejection && (
        <div className="rejection">
          <h3>거절 사유</h3>
          <p>
            {REJECT_REASON[c.rejection.reason_code] ?? c.rejection.reason_code} <code>{c.rejection.reason_code}</code>
            {' · '}
            {actorName.get(c.rejection.actor_id) ?? c.rejection.actor_id} · Context v{c.rejection.context_version}
          </p>
          {c.rejection.target_task_ids.length > 0 && (
            <p className="small">대상 {c.rejection.target_task_ids.join(', ')}</p>
          )}
          {c.rejection.comment && <p className="small">사유 “{c.rejection.comment}”</p>}
          <p className="small">
            거절은 작업을 고정하지 않습니다(같은 배정만 다시 제안하지 않음). 작업을 그대로 두려면 타임라인에서
            고정하세요.
          </p>
        </div>
      )}

      <FactChanges changes={c.fact_changes} base={c.base_plan_revision} state={state} />

      <h3>변경점</h3>
      {c.changes.length === 0 ? (
        <p className="muted">
          변경 없음
          {c.fact_changes.length > 0 && ' — 배치는 그대로이고, 위의 바뀐 사실 때문에 다시 확정합니다.'}
        </p>
      ) : (
        <table className="tbl">
          <tbody>
            {c.changes.map((ch) => (
              <tr key={ch.task_id}>
                <th>{ch.task_id}</th>
                <td>{assign(ch.before)}</td>
                <td>→</td>
                <td className="strong">
                  {assign(ch.after)}
                  {ch.before.resource_id !== ch.after.resource_id && <span className="tag tag-resource"> 자원 바뀜</span>}
                </td>
                <td className="small">
                  {ch.delay > 0
                    ? `기준에서 ${movedOf(c, ch.task_id) || delayText(ch.delay, ch.work_delay)}`
                    : ''}
                </td>
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
                <th>목적 순서</th>
                <td>{OBJECTIVE[c.solver.objective] ?? c.solver.objective}</td>
              </tr>
              {c.solver.objective === 'DELAY_FIRST' ? (
                <>
                  <tr>
                    <th>1단계 (기준에서 옮긴 거리)</th>
                    <td>
                      {SOLVER_STATUS[c.solver.stage1.status] ?? c.solver.stage1.status} · 옮긴 거리 합{' '}
                      {c.solver.stage1.delay ?? '—'}분 <code>{c.solver.stage1.status}</code>
                    </td>
                  </tr>
                  <tr>
                    <th>2단계 (변경 수)</th>
                    <td>
                      {c.solver.stage2 ? (
                        <>
                          {SOLVER_STATUS[c.solver.stage2.status] ?? c.solver.stage2.status} · 변경{' '}
                          {c.solver.stage2.changed ?? '—'} · 근무시간 기준 옮긴 거리{' '}
                          {c.solver.stage2.work_delay ?? '—'}분 <code>{c.solver.stage2.status}</code>
                        </>
                      ) : (
                        '실행 안 함'
                      )}
                    </td>
                  </tr>
                </>
              ) : (
                <>
                  <tr>
                    <th>1단계 (변경 수)</th>
                    <td>
                      {SOLVER_STATUS[c.solver.stage1.status] ?? c.solver.stage1.status} · 변경{' '}
                      {c.solver.stage1.changed ?? '—'} <code>{c.solver.stage1.status}</code>
                    </td>
                  </tr>
                  <tr>
                    <th>2단계 (기준에서 옮긴 거리)</th>
                    <td>
                      {c.solver.stage2 ? (
                        <>
                          {SOLVER_STATUS[c.solver.stage2.status] ?? c.solver.stage2.status} · 옮긴 거리 합{' '}
                          {delayText(c.solver.stage2.delay, c.solver.stage2.work_delay)}{' '}
                          <code>{c.solver.stage2.status}</code>
                        </>
                      ) : (
                        '실행 안 함'
                      )}
                    </td>
                  </tr>
                </>
              )}
            </tbody>
          </table>
          {c.solver.stage2?.resource_changed !== null && c.solver.stage2?.resource_changed !== undefined && (
            <p className="small">
              마지막 단계: 자원을 바꾸는 작업 {c.solver.stage2.resource_changed}건{' '}
              <span className="muted">(변경 수와 옮긴 거리가 같은 해 가운데 가장 적게)</span>
            </p>
          )}
          <p className="small">
            {c.solver.minimal_change && <span className="tag">최소 변경(이 탐색 범위 안)</span>}
            {c.solver.minimal_delay && <span className="tag">기준에서 가장 덜 옮김(이 탐색 범위 안)</span>}
            {c.solver.delay_optimality_unconfirmed && (
              <span className="tag tag-warn">
                {c.solver.objective === 'DELAY_FIRST' ? '변경 수 최적성 미확정' : '옮긴 거리 최적성 미확정'}
              </span>
            )}
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
                  <td>
                    {ITEM_STATUS[it.item_status] ?? it.item_status}
                    {it.answer_source?.prior && (
                      <div className="small muted">
                        이전 답 적용 ·{' '}
                        {actorName.get(it.answer_source.actor_id ?? '') ?? it.answer_source.actor_id}
                        {it.answer_source.at && ` · ${it.answer_source.at}`} · 후보{' '}
                        <code>{it.answer_source.candidate_id}</code>
                      </div>
                    )}
                    {it.request?.quoted_comment && (
                      <div className="small muted">이견(인용): “{it.request.quoted_comment}”</div>
                    )}
                  </td>
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
              onClick={async () => {
                const r = await run('협의 항목 수용(WAIVE)', `/consultations/${c.candidate_id}/waive`, {
                  task_ids: waiveIds,
                  comment: waiveComment,
                })
                // 수용된 항목을 다시 보내 ITEM_NOT_WAIVABLE이 나지 않게 선택을 비운다
                if (r && (r.status === 'APPLIED' || r.status === 'REPLAYED')) setWaiveIds([])
              }}
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
          <label>
            사유
            <input value={rejComment} onChange={(e) => setRejComment(e.target.value)} />
          </label>
        </div>
        <div className="row">
          {/* 시연값은 scenario(DEMO_MODE)에서만 받는다 */}
          {scenario?.rejections.map((x) => (
            <button
              key={x.label}
              type="button"
              onClick={() => {
                setRejReason(x.body.reason_code)
                setRejTargets(x.body.target_task_ids)
                setRejComment(x.body.comment)
              }}
            >
              시연값: {x.label}
            </button>
          ))}
          <button
            className="btn-warn"
            disabled={!isSupervisor || busy !== null}
            title={needSup}
            onClick={() =>
              run('후보 거절', `/candidates/${c.candidate_id}/reject`, {
                validation_id: v?.validation_id ?? '',
                reason_code: rejReason,
                target_task_ids: rejTargets,
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
