// 받은 요청. X-Actor 본인에게 온 질문만 state.inbox로 받는다.
// 서버 문구(답의 기준)를 먼저, 모델이 쓴 설명은 아래에 따로 구분해 보여 준다.

import { useState } from 'react'
import type { InboxItem, SiteState } from '../types'
import {
  DECISION,
  DECISION_BY_TYPE,
  FACT_FIELD,
  MESSAGE_STATUS,
  MESSAGE_TYPE,
  PROPOSAL_STATUS,
} from '../labels'
import { useEnv } from '../context'
import { ANSWERABLE } from '../inbox'
import type { Run } from './ReviewPanel'

interface Props {
  state: SiteState
  busy: string | null
  run: Run
}

export function Inbox({ state, busy, run }: Props) {
  if (state.inbox.length === 0) return <p className="muted">받은 요청이 없습니다.</p>
  return (
    <div className="inbox">
      {state.inbox.map((m) =>
        m.request_group_id !== null ? (
          <RequestCard key={m.request_group_id} m={m} busy={busy} run={run} />
        ) : (
          <InboxCard key={m.message_id} m={m} busy={busy} run={run} />
        ),
      )}
    </div>
  )
}

type Answer = { decision: 'ACCEPT' | 'DECLINE' | null; comment: string }

/** 변경 요청 한 통: 담당자 한 명에게 온 항목 전부. 항목마다 수락·이견을 정해 한 번에 보낸다.
 *  표의 값(작업, 전→후)은 서버가 협의 항목에서 채운 것이고, Agent 설명은 한 번만 보인다. */
function RequestCard({ m, busy, run }: { m: InboxItem; busy: string | null; run: Run }) {
  const { clock } = useEnv()
  const [answers, setAnswers] = useState<Record<string, Answer>>({})
  const words = DECISION_BY_TYPE.CHANGE_REQUEST
  const open = m.status === 'OPEN'
  const done = m.status === 'LATE' || m.status === 'CANCELLED'
  const of = (id: string): Answer => answers[id] ?? { decision: null, comment: '' }
  const set = (id: string, a: Partial<Answer>) => setAnswers({ ...answers, [id]: { ...of(id), ...a } })
  // 모든 항목을 정하고 이견마다 사유가 있어야 보낸다(서버도 REPLY_INCOMPLETE·COMMENT_REQUIRED로 막는다)
  const ready = m.items.every((i) => {
    const a = of(i.message_id)
    return a.decision === 'ACCEPT' || (a.decision === 'DECLINE' && a.comment.trim() !== '')
  })
  const send = () =>
    run(`변경 요청 답 (${m.items.length}건)`, `/requests/${m.request_group_id}/reply`, {
      answers: m.items.map((i) => {
        const a = of(i.message_id)
        return { message_id: i.message_id, decision: a.decision, comment: a.decision === 'DECLINE' ? a.comment : '' }
      }),
    })
  return (
    <article className={`inbox-item ${done ? 'inbox-done' : ''} ${open ? 'inbox-open' : ''}`}>
      <header className="row">
        <span className={`badge inbox-${m.status.toLowerCase()}`}>{MESSAGE_STATUS[m.status] ?? m.status}</span>
        <strong className="inbox-title">
          {MESSAGE_TYPE.CHANGE_REQUEST} {m.items.length}건
        </strong>
        <span className="small muted">
          {m.plan_label ?? '후보'} · <code>{m.candidate_id ?? '—'}</code>
        </span>
      </header>
      {m.agent_text && (
        <div className="model-block">
          <div className="block-label">Agent 설명(모델 작성)</div>
          <p>{m.agent_text}</p>
        </div>
      )}
      <div className="server-block">
        <div className="block-label">바뀌는 작업 (서버 값 · 답의 기준)</div>
        <table className="tbl small request-items">
          <thead>
            <tr>
              <th>작업</th>
              <th>시각 (전 → 후)</th>
              <th>자원 (전 → 후)</th>
              <th>답</th>
            </tr>
          </thead>
          <tbody>
            {m.items.map((i) => {
              const a = of(i.message_id)
              const moved = i.before && i.after && i.before.start !== i.after.start
              const swapped = i.before && i.after && i.before.resource_id !== i.after.resource_id
              return (
                <tr key={i.message_id}>
                  <th>{i.task_id ?? '—'}</th>
                  <td>
                    {i.before && i.after ? (
                      moved ? (
                        <>
                          {clock.format(i.before.start)} → <b>{clock.format(i.after.start)}</b>
                        </>
                      ) : (
                        <span className="muted">{clock.format(i.after.start)} 그대로</span>
                      )
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>
                    {i.before && i.after ? (
                      swapped ? (
                        <>
                          {i.before.resource_id ?? '없음'} → <b>{i.after.resource_id ?? '없음'}</b>
                        </>
                      ) : (
                        <span className="muted">{i.after.resource_id ?? '없음'} 그대로</span>
                      )
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>
                    {open ? (
                      <div className="request-answer">
                        <label className="inline">
                          <input
                            type="radio"
                            name={`ans:${i.message_id}`}
                            checked={a.decision === 'ACCEPT'}
                            onChange={() => set(i.message_id, { decision: 'ACCEPT' })}
                          />
                          {words.ACCEPT}
                        </label>
                        <label className="inline">
                          <input
                            type="radio"
                            name={`ans:${i.message_id}`}
                            checked={a.decision === 'DECLINE'}
                            onChange={() => set(i.message_id, { decision: 'DECLINE' })}
                          />
                          {words.DECLINE}
                        </label>
                        {a.decision === 'DECLINE' && (
                          <input
                            className="grow"
                            placeholder="이견 사유(필수)"
                            value={a.comment}
                            onChange={(e) => set(i.message_id, { comment: e.target.value })}
                          />
                        )}
                      </div>
                    ) : i.reply ? (
                      <>
                        {words[i.reply.decision] ?? i.reply.decision}
                        {i.reply.comment ? ` “${i.reply.comment}”` : ''}
                      </>
                    ) : (
                      <span className="muted">답 없음</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="small muted">
        보낸 Run <code>{m.run_id}</code> · step {m.step_no} · Context v{m.created_context_version}
        {done && ' · 이 요청은 효력이 없습니다(안이 바뀌었거나 협의가 끝났습니다)'}
      </p>
      {open && (
        <div className="row">
          <button
            disabled={busy !== null}
            onClick={() =>
              setAnswers(
                Object.fromEntries(m.items.map((i) => [i.message_id, { decision: 'ACCEPT', comment: '' }])),
              )
            }
          >
            모두 {words.ACCEPT}
          </button>
          <span className="grow" />
          <button
            className="btn-primary"
            disabled={busy !== null || !ready}
            title={ready ? undefined : '모든 항목에 수락·이견을 정하고, 이견에는 사유를 적어야 합니다'}
            onClick={() => void send()}
          >
            보내기
          </button>
        </div>
      )}
    </article>
  )
}

function InboxCard({ m, busy, run }: { m: InboxItem; busy: string | null; run: Run }) {
  const [comment, setComment] = useState('')
  const { clock } = useEnv()
  const answerable = ANSWERABLE.includes(m.type)
  // 확인 메시지는 제안 유형으로 나눈다: 사실 수정
  const kind = m.proposal_type === 'FACT_UPDATE' ? 'FACT_UPDATE' : m.type
  const open = m.status === 'OPEN' && answerable
  const done = m.status === 'LATE' || m.status === 'CANCELLED'
  const words = DECISION_BY_TYPE[kind] ?? DECISION
  // 변경 요청의 이견은 사유가 필요하다(서버도 COMMENT_REQUIRED로 막는다)
  const needReason = m.type === 'CHANGE_REQUEST'
  const reply = (decision: 'ACCEPT' | 'DECLINE') =>
    run(`${MESSAGE_TYPE[kind] ?? '받은 요청'} ${words[decision]}`, `/messages/${m.message_id}/reply`, {
      decision,
      comment,
    })
  const statusText = answerable ? (MESSAGE_STATUS[m.status] ?? m.status) : (MESSAGE_TYPE[m.type] ?? m.type)
  return (
    <article className={`inbox-item ${done ? 'inbox-done' : ''} ${open ? 'inbox-open' : ''}`}>
      <header className="row">
        <span className={`badge inbox-${answerable ? m.status.toLowerCase() : 'notice'}`}>{statusText}</span>
        <strong className="inbox-title">{MESSAGE_TYPE[kind] ?? m.type}</strong>
        <span className="small muted">
          <code>{m.message_id}</code>
        </span>
      </header>
      <div className="server-block">
        <div className="block-label">서버 문구 ({answerable ? '답의 기준' : '확정 내용'})</div>
        <p>{m.body}</p>
      </div>
      {m.agent_text && (
        <div className="model-block">
          <div className="block-label">Agent 설명(모델 작성)</div>
          <p>{m.agent_text}</p>
        </div>
      )}
      <table className="tbl small">
        <tbody>
          {m.type === 'CHANGE_REQUEST' && (
            <tr>
              <th>후보</th>
              <td>
                <code>{m.candidate_id ?? '—'}</code>
              </td>
            </tr>
          )}
          {kind === 'FACT_UPDATE' && m.fact && (
            <tr>
              <th>{FACT_FIELD[m.fact.field] ?? m.fact.field}</th>
              <td>
                {m.task_id ?? '—'} · {clock.format(m.fact.old_value)} → <b>{clock.format(m.fact.new_value)}</b>
              </td>
            </tr>
          )}
          <tr>
            <th>보낸 Run</th>
            <td>
              {m.run_id ? (
                <>
                  <code>{m.run_id}</code> · step {m.step_no}
                </>
              ) : (
                '서버'
              )}{' '}
              · Context v{m.created_context_version}
            </td>
          </tr>
          {answerable && (
            <tr>
              <th>상태</th>
              <td>
                {MESSAGE_STATUS[m.status] ?? m.status}
                {m.proposal_status && ` · 제안 ${PROPOSAL_STATUS[m.proposal_status] ?? m.proposal_status}`}
                {m.reply &&
                  ` · 답 ${words[m.reply.decision] ?? m.reply.decision}${m.reply.comment ? ` “${m.reply.comment}”` : ''}`}
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {open && (
        <div className="row">
          <input
            className="grow"
            placeholder={needReason ? '이견 사유(이견이면 필수)' : '사유(선택)'}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
          />
          <button className="btn-primary" disabled={busy !== null} onClick={() => void reply('ACCEPT')}>
            {words.ACCEPT}
          </button>
          <button
            className="btn-warn"
            disabled={busy !== null || (needReason && !comment.trim())}
            title={needReason && !comment.trim() ? '이견 사유를 적어야 합니다' : undefined}
            onClick={() => void reply('DECLINE')}
          >
            {words.DECLINE}
          </button>
        </div>
      )}
    </article>
  )
}
