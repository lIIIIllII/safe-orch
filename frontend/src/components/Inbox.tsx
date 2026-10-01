// 받은 요청 (부록 A.21 7). X-Actor 본인에게 온 질문만 state.inbox로 받는다.
// 서버 문구(동의 내용의 기준)를 먼저, 모델이 쓴 설명은 아래에 따로 구분해 보여 준다(§11.6).

import { useState } from 'react'
import type { InboxItem, SiteState } from '../types'
import { AXIS, DECISION, MESSAGE_STATUS, PROPOSAL_STATUS } from '../labels'
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
  const open = m.status === 'OPEN'
  const done = m.status === 'LATE' || m.status === 'CANCELLED'
  const reply = (decision: 'ACCEPT' | 'DECLINE') =>
    run(`받은 요청 ${DECISION[decision]}`, `/messages/${m.message_id}/reply`, { decision, comment })
  return (
    <article className={`inbox-item ${done ? 'inbox-done' : ''} ${open ? 'inbox-open' : ''}`}>
      <header className="row">
        <span className={`badge inbox-${m.status.toLowerCase()}`}>{MESSAGE_STATUS[m.status] ?? m.status}</span>
        <span className="small muted">
          {m.type} · <code>{m.message_id}</code>
        </span>
      </header>
      <div className="server-block">
        <div className="block-label">서버 문구 (동의 내용의 기준)</div>
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
          <tr>
            <th>보낸 Run</th>
            <td>
              <code>{m.run_id}</code> · step {m.step_no} · Context v{m.created_context_version}
            </td>
          </tr>
          <tr>
            <th>상태</th>
            <td>
              {MESSAGE_STATUS[m.status] ?? m.status}
              {m.proposal_status && ` · 제안 ${PROPOSAL_STATUS[m.proposal_status] ?? m.proposal_status}`}
              {m.reply &&
                ` · 답 ${DECISION[m.reply.decision] ?? m.reply.decision}${m.reply.comment ? ` “${m.reply.comment}”` : ''}`}
            </td>
          </tr>
        </tbody>
      </table>
      {open && (
        <div className="row">
          <input
            className="grow"
            placeholder="사유(선택)"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
          />
          <button className="btn-primary" disabled={busy !== null} onClick={() => void reply('ACCEPT')}>
            수락
          </button>
          <button className="btn-warn" disabled={busy !== null} onClick={() => void reply('DECLINE')}>
            거절
          </button>
        </div>
      )}
    </article>
  )
}
