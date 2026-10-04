// Activity. Run 목록 + AgentStep 카드.
// 모델 문장(Decision Summary)과 서버 결과(Tool 결과·Guard)를 블록으로 나눈다.

import { useEffect, useState } from 'react'
import { fetchSteps } from '../api'
import type { AgentStep, RunSummary, SiteState } from '../types'
import {
  ACTION_NAME,
  AGENT_TYPE,
  AXIS,
  EXCLUDE_REASON,
  RESULT_KIND,
  RUN_STATUS,
  SCOPE_LEVEL,
  SOLVER_STATUS,
  STEP_STATUS,
  WAIT_KIND,
  endReason,
} from '../labels'
import { Code } from './common'
import { attributeName, useEnv } from '../context'
import { delayText } from '../time'
import type { Run } from './ReviewPanel'

interface Props {
  state: SiteState
  actorId: string
  selectedRunId: string | null
  onSelectRun: (id: string) => void
  isSupervisor: boolean
  busy: string | null
  run: Run
  refreshKey: number
}

const BUDGET_MAX: Record<string, Record<string, number>> = {
  MAIN: { steps: 14, agent_calls: 10 },
  REPLANNING: { steps: 15, solver_calls: 6 },
}

/** 메인 Run 바로 아래에 그 메인이 부른 하위 Run을 둔다(서버가 준 순서는 그대로). */
function withChildren(runs: RunSummary[]): RunSummary[] {
  const ids = new Set(runs.map((r) => r.run_id))
  const top = runs.filter((r) => r.parent_run_id === null || !ids.has(r.parent_run_id))
  return top.flatMap((r) => [r, ...runs.filter((c) => c.parent_run_id === r.run_id)])
}

export function Activity({ state, actorId, selectedRunId, onSelectRun, isSupervisor, busy, run, refreshKey }: Props) {
  const current = state.runs.find((r) => r.run_id === selectedRunId) ?? null
  return (
    <section className="panel activity">
      <div className="panel-head">
        <h2>Activity</h2>
        <span className="muted">Run {state.runs.length}건</span>
      </div>
      <div className="runs">
        {state.runs.length === 0 && <p className="muted">아직 Run이 없습니다.</p>}
        {withChildren(state.runs).map((r) => (
          <RunRow
            key={r.run_id}
            r={r}
            on={r.run_id === selectedRunId}
            onClick={() => onSelectRun(r.run_id)}
            canCancel={isSupervisor}
            busy={busy}
            run={run}
          />
        ))}
      </div>
      <div className="steps">
        {current ? (
          <Steps run={current} actorId={actorId} refreshKey={refreshKey} />
        ) : (
          <p className="muted">Run을 선택하세요.</p>
        )}
      </div>
    </section>
  )
}

function RunRow({
  r,
  on,
  onClick,
  canCancel,
  busy,
  run,
}: {
  r: RunSummary
  on: boolean
  onClick: () => void
  canCancel: boolean
  busy: string | null
  run: Run
}) {
  const max = r.budget_max ?? BUDGET_MAX[r.agent_type] ?? {}
  const active = ['RUNNING', 'WAITING_HUMAN', 'ERROR'].includes(r.status)
  return (
    <div className={`run-row ${on ? 'run-on' : ''} ${r.parent_run_id ? 'run-child' : ''}`} onClick={onClick}>
      <span className="strong">
        {r.parent_run_id && '└ '}
        {AGENT_TYPE[r.agent_type] ?? r.agent_type}
      </span>
      <span className={`badge run-${r.status.toLowerCase()}`}>{RUN_STATUS[r.status] ?? r.status}</span>
      {r.resume_count > 0 && <span className="badge badge-resume">재개 {r.resume_count}회</span>}
      {r.wait_kind && (
        <span className="small">
          {WAIT_KIND[r.wait_kind] ?? r.wait_kind}
          {r.wait_ref && ` (${r.wait_ref.slice(0, 13)})`}
        </span>
      )}
      <span className="small muted">
        step {r.last_step_no}
        {r.current_step_status && ` · ${STEP_STATUS[r.current_step_status] ?? r.current_step_status}`}
        {' · '}Budget step {r.budget_used.steps ?? 0}/{max.steps ?? '—'} · Solver {r.budget_used.solver_calls ?? 0}/
        {max.solver_calls ?? '—'}
        {(r.budget_used.human_rounds ?? 0) > 0 &&
          ` · 사람 확인 ${r.budget_used.human_rounds}/${max.human_rounds ?? '—'}`}
        {max.agent_calls !== undefined && ` · 호출 ${r.budget_used.agent_calls ?? 0}/${max.agent_calls}`}
      </span>
      {r.end_reason && <span className="small">{endReason(r.end_reason)}</span>}
      {active && (
        <button
          className="btn-small"
          disabled={!canCancel || busy !== null}
          title={canCancel ? undefined : 'Supervisor 권한 필요'}
          onClick={(e) => {
            e.stopPropagation()
            if (window.confirm(`Run ${r.run_id}을(를) 취소할까요?`)) {
              void run('Run 취소', `/runs/${r.run_id}/cancel`, undefined)
            }
          }}
        >
          취소
        </button>
      )}
    </div>
  )
}

function Steps({ run, actorId, refreshKey }: { run: RunSummary; actorId: string; refreshKey: number }) {
  const [steps, setSteps] = useState<AgentStep[]>([])
  const [error, setError] = useState<string | null>(null)
  // 선택한 Run의 step 번호·상태가 바뀔 때만 다시 가져온다
  const fetchKey = `${run.run_id}:${run.last_step_no}:${run.current_step_status}:${run.status}:${refreshKey}`
  useEffect(() => {
    let alive = true
    fetchSteps(actorId, run.run_id)
      .then((s) => {
        if (alive) {
          setSteps(s)
          setError(null)
        }
      })
      .catch((e: unknown) => alive && setError(String(e)))
    return () => {
      alive = false
    }
  }, [fetchKey, actorId, run.run_id])

  const shown = steps.filter((s) => s.run_id === run.run_id)
  const goal = shown.find((s) => s.goal)?.goal
  return (
    <>
      <div className="run-goal">
        <span className="muted small">{run.run_id}</span>
        {run.resume_count > 0 && <span className="badge badge-resume">재개 {run.resume_count}회</span>}
        {goal && (
          <p>
            <b>Goal</b> {goal}
          </p>
        )}
        {error && <p className="bad small">step 조회 실패: {error}</p>}
      </div>
      {[...shown].reverse().map((s) => (
        <StepCard key={s.step_no} s={s} />
      ))}
    </>
  )
}

function actionNames(available: unknown): string[] {
  if (!Array.isArray(available)) return []
  return available.map((a: unknown) => {
    const o = a as { name?: string; title?: string; function?: { name?: string } }
    return o?.name ?? o?.function?.name ?? o?.title ?? JSON.stringify(a)
  })
}

function splitSummary(text: string): { why: string; next: string | null } {
  const m = /이유\s*[:：]\s*([\s\S]*?)\s*[/／]\s*다음\s*[:：]\s*([\s\S]*)/.exec(text)
  return m ? { why: m[1], next: m[2] } : { why: text, next: null }
}

function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x)
}

type Why = { reason: string; attribute?: string }
type Rid = { resource_id: string; reasons?: Why[] }

/** 자원 조회 결과: 배정 가능 자원과 제외 자원·이유. */
function ListResult({ tr }: { tr: Record<string, unknown> }) {
  const { meta } = useEnv()
  const assignable = (tr.assignable as Rid[]) ?? []
  const excluded = (tr.excluded as Rid[]) ?? []
  return (
    <table className="tbl small">
      <tbody>
        {'task_id' in tr && (
          <tr>
            <th>조회 작업</th>
            <td>
              {String(tr.task_id)} · 필요 유형 {String(tr.required_type ?? '—')} · 현재 {String(tr.current ?? '없음')}
            </td>
          </tr>
        )}
        {typeof tr.assignable_in_other_types === 'number' && (
          <tr>
            <th>다른 유형</th>
            <td>다른 유형에서 쓸 수 있는 자원 {tr.assignable_in_other_types}개</td>
          </tr>
        )}
        <tr>
          <th>배정 가능</th>
          <td>
            {assignable.length === 0
              ? '없음'
              : assignable.map((a) => (
                  <span key={a.resource_id} className="tag">
                    {a.resource_id}
                    {a.resource_id === tr.current ? ' (현재)' : ''}
                  </span>
                ))}
          </td>
        </tr>
        <tr>
          <th>제외</th>
          <td>
            {excluded.length === 0
              ? '없음'
              : excluded.map((a) => (
                  <span key={a.resource_id} className="tag tag-warn">
                    {a.resource_id} —{' '}
                    {(a.reasons ?? []).map((w, i) => (
                      <span key={`${w.reason}:${w.attribute ?? ''}`}>
                        {i > 0 && ', '}
                        {EXCLUDE_REASON[w.reason] ?? w.reason}
                        {w.attribute && `(${attributeName(meta, w.attribute)})`} <code>{w.reason}</code>
                      </span>
                    ))}
                  </span>
                ))}
          </td>
        </tr>
      </tbody>
    </table>
  )
}

/** 담당자 확인 요청: 메시지와 서버 문구. 모델의 question은 모델 블록으로 따로 둔다. */
function AskResult({ tr, args }: { tr: Record<string, unknown>; args: Record<string, unknown> }) {
  // 사전 확인(ASK_OWNER)은 need 하나를 묻는다. 옛 기록(ASK_TASK_OWNER)은 작업·축·허용 값을 인자로 가졌다
  const values = Array.isArray(args.allowed_values) ? (args.allowed_values as string[]) : []
  const text = typeof args.message === 'string' ? args.message : args.question
  return (
    <>
      <p>
        메시지 <code>{String(tr.message_id)}</code> → {String(tr.to_actor_id)}
        {typeof args.task_id === 'string' && (
          <>
            {' '}
            · 작업 {args.task_id} · {AXIS[String(args.axis)] ?? String(args.axis)} 축 · 허용 값 {values.join(', ')}
          </>
        )}
      </p>
      <p className="ask-body">서버 문구: {String(tr.body)}</p>
      {typeof text === 'string' && (
        <div className="model-block">
          <div className="block-label">Agent 설명(모델 작성)</div>
          <p>{text}</p>
        </div>
      )}
    </>
  )
}

function StepCard({ s }: { s: AgentStep }) {
  const args = { ...(s.action?.args ?? {}) }
  delete args.decision_summary
  const headArgs = { ...args }
  delete headArgs.question
  const name = s.action?.name ?? '—'
  const level = typeof args.level === 'string' ? args.level : null
  const tr = s.tool_result ?? {}
  const stage1 = tr.stage1 as { status?: string; changed?: number } | undefined
  // work_delay는 steps API가 조회 시 붙인다(저장하지 않음). 표기는 검토 패널과 같다.
  const stage2 = tr.stage2 as { status?: string; delay?: number; work_delay?: number | null } | null | undefined
  const summary = s.decision_summary ? splitSummary(s.decision_summary) : null
  const rest = Object.fromEntries(
    Object.entries(tr).filter(
      ([k]) =>
        ![
          'stage1',
          'stage2',
          'scope_level',
          'spec_hash',
          'snapshot_id',
          'chosen_stage',
          'minimal_change',
          'delay_optimality_unconfirmed',
          'candidate_id',
          // LIST·ASK·TRY는 아래 전용 블록으로 보여 준다
          'task_id',
          'required_type',
          'current',
          'assignable',
          'excluded',
          'resources_hash',
          'proposal_id',
          'message_id',
          'to_actor_id',
          'body',
          'try_resources',
          'assignable_in_other_types',
        ].includes(k),
    ),
  )
  return (
    <article className={`step step-${s.status.toLowerCase()}`}>
      <header className="step-head">
        <b>#{s.step_no}</b>
        <span className="badge">{STEP_STATUS[s.status] ?? s.status}</span>
        <span className="strong">{ACTION_NAME[name] ?? name}</span>
        <code>{name}</code>
        {level && <span className="small">{SCOPE_LEVEL[level] ?? level}</span>}
        {Object.keys(headArgs).length > 0 && !level && <code className="small">{JSON.stringify(headArgs)}</code>}
      </header>

      {s.status === 'RESERVED' && <p className="muted">모델 판단 중</p>}
      {s.status === 'ABORTED' && (
        <p className="bad small">중단: {s.abort_reason ? <Code code={s.abort_reason} /> : '—'}</p>
      )}

      {summary && (
        <div className="model-block">
          <div className="block-label">모델 설명 (LLM 작성, 판정 근거 아님)</div>
          <p>
            <b>이유</b> {summary.why}
          </p>
          {summary.next && (
            <p>
              <b>다음</b> {summary.next}
            </p>
          )}
        </div>
      )}

      {s.status !== 'RESERVED' && (
        <div className="server-block">
          <div className="block-label">서버 결과</div>
          {stage1 && (
            <p>
              1단계 {SOLVER_STATUS[stage1.status ?? ''] ?? stage1.status} <code>{stage1.status}</code> · 변경{' '}
              {stage1.changed ?? '—'}
              {stage2 && (
                <>
                  {' '}
                  / 2단계 {SOLVER_STATUS[stage2.status ?? ''] ?? stage2.status} <code>{stage2.status}</code> · 지연{' '}
                  {delayText(stage2.delay, stage2.work_delay)}
                </>
              )}
              {tr.delay_optimality_unconfirmed === true && <span className="tag tag-warn">지연 최적성 미확정</span>}
            </p>
          )}
          {(name === 'LIST_ASSIGNABLE_RESOURCES' || name === 'LOOKUP_RESOURCE') && 'assignable' in tr && (
            <ListResult tr={tr} />
          )}
          {(name === 'ASK_OWNER' || name === 'ASK_TASK_OWNER') && 'message_id' in tr && (
            <AskResult tr={tr} args={args} />
          )}
          {name === 'TRY_ALTERNATIVE_RESOURCE' && isRecord(tr.try_resources) && (
            <p>
              대체 자원 시도{' '}
              {Object.entries(tr.try_resources)
                .map(([t, rs]) => `${t} → ${(rs as string[]).join(', ')}`)
                .join(' · ')}{' '}
              <span className="muted small">(범위 L0 + 대체 자원)</span>
            </p>
          )}
          {'candidate_id' in tr && (
            <p>
              후보 <code>{String(tr.candidate_id ?? '없음')}</code>
            </p>
          )}
          {Object.keys(rest).length > 0 && <pre className="small">{JSON.stringify(rest, null, 1)}</pre>}
          <p>
            Guard{' '}
            {s.guard ? (
              <>
                <span className={s.guard.verdict === 'ACCEPTED' ? 'good' : 'bad'}>
                  {s.guard.verdict === 'ACCEPTED' ? '허용' : '거절'}
                </span>{' '}
                {s.guard.reason_code && <Code code={s.guard.reason_code} />}
              </>
            ) : (
              '—'
            )}
            {' · '}결과 {s.result_kind ? (RESULT_KIND[s.result_kind] ?? s.result_kind) : '—'}
          </p>
          {Array.isArray(s.state_changes) || (s.state_changes && typeof s.state_changes === 'object') ? (
            <p className="small muted wrap">상태 변경 {JSON.stringify(s.state_changes)}</p>
          ) : null}
        </div>
      )}

      <footer className="step-foot small muted">
        관찰 ctx v{s.observed_context_version}·R{s.observed_plan_revision}
        {s.budget_remaining &&
          ` · 잔여 ${Object.entries(s.budget_remaining)
            .map(([k, v]) => `${k} ${v}`)
            .join(', ')}`}
        {s.model_id && ` · ${s.model_id}`}
        {s.prompt_version && ` · ${s.prompt_version}`}
        {s.llm_attempts != null && ` · LLM 시도 ${s.llm_attempts}`}
        {s.created_at && ` · ${s.created_at}`}
      </footer>
      <details>
        <summary className="small">관찰·허용 행동</summary>
        <p className="small">허용 행동: {actionNames(s.available_actions).join(', ') || '—'}</p>
        <pre className="small">{JSON.stringify(s.observation, null, 1)}</pre>
      </details>
    </article>
  )
}
