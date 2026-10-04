// 받은 요청. X-Actor 본인에게 온 질문만 state.inbox로 받는다.
// 서버 문구(동의 내용의 기준)를 먼저, 모델이 쓴 설명은 아래에 따로 구분해 보여 준다.

import { useState } from 'react'
import type { InboxItem, SiteState } from '../types'
import {
  AXIS,
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
      {state.inbox.map((m) => (
        <InboxCard key={m.message_id} m={m} busy={busy} run={run} />
      ))}
    </div>
  )
}

function InboxCard({ m, busy, run }: { m: InboxItem; busy: string | null; run: Run }) {
  const [comment, setComment] = useState('')
  const { clock } = useEnv()
  const answerable = ANSWERABLE.includes(m.type)
  // 확인 메시지는 제안 유형으로 나눈다: 사실 수정
  // 제안 없는 질문은 자유 텍스트 답이다(신고자 확인 질문)
  const kind =
    m.proposal_type === 'FACT_UPDATE'
      ? 'FACT_UPDATE'
      : m.type === 'QUESTION' && !m.proposal_id
        ? 'FREE_QUESTION'
        : m.type
  const free = kind === 'FREE_QUESTION'
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
          {kind === 'QUESTION' && (
            <>
              <tr>
                <th>작업</th>
                <td>
                  {m.task_id ?? '—'} · {m.axis ? (AXIS[m.axis] ?? m.axis) : '—'} 축
                </td>
              </tr>
              <tr>
                <th>허용 값</th>
                <td>{m.allowed_values.join(', ') || '—'}</td>
              </tr>
            </>
          )}
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
      {open && free && (
        <div className="row">
          <textarea
            className="grow"
            rows={2}
            placeholder="답을 문장으로 적습니다(필수)"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
          />
          <button
            className="btn-primary"
            disabled={busy !== null || !comment.trim()}
            onClick={() =>
              void run(`${MESSAGE_TYPE[kind]} 답변`, `/messages/${m.message_id}/reply`, {
                decision: 'ANSWER',
                comment,
              })
            }
          >
            답변 보내기
          </button>
        </div>
      )}
      {open && !free && (
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
