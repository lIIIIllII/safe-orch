import type { CommandOutcome } from '../types'
import { REASON, VALIDATION_BADGE } from '../labels'
import { ruleName, useEnv } from '../context'

/** 한국어 문구 + 원래 코드. Pack Rule은 meta 표시 이름, 공통 코드는 코드표. 둘 다 없으면 코드만. */
export function Code({ code }: { code: string }) {
  const { meta } = useEnv()
  const text = ruleName(meta, code) ?? REASON[code]
  return (
    <span className="code-label">
      {text && <span>{text}</span>}
      <code>{code}</code>
    </span>
  )
}

export function ValidationBadge({ status }: { status: string }) {
  const b = VALIDATION_BADGE[status]
  if (!b) return <span className="badge">{status}</span>
  return (
    <span className={`badge badge-${b.cls}`}>
      <span aria-hidden="true">{b.icon}</span> {b.text}
    </span>
  )
}

export function OutcomeBox({ outcome }: { outcome: CommandOutcome | null }) {
  if (!outcome) return null
  const r = outcome.response
  const ok = r.status === 'APPLIED' || r.status === 'REPLAYED'
  return (
    <div className={`outcome ${ok ? 'outcome-ok' : 'outcome-bad'}`}>
      <div className="outcome-head">
        <strong>{outcome.label}</strong>
        <span>
          {outcome.actor_id} · <Code code={r.status} /> · HTTP {r.http ?? '—'}
        </span>
      </div>
      {/* 폼 응답이 queued면 대기열 접수로 알린다. 폼 응답만 form_id를 갖는다. */}
      {ok && 'form_id' in r.result_refs && r.result_refs.queued === true && (
        <p className="queued-note">
          대기열에 접수됨 — 앞 Case가 끝나면 접수 순서대로 재검사됩니다 ({String(r.result_refs.task_id)})
        </p>
      )}
      {r.reason_codes.length > 0 && (
        <ul className="outcome-reasons">
          {r.reason_codes.map((c) => (
            <li key={c}>
              <Code code={c} />
            </li>
          ))}
        </ul>
      )}
      {r.detail !== undefined && (
        <details>
          <summary>상세</summary>
          <pre>{JSON.stringify(r.detail, null, 2)}</pre>
        </details>
      )}
    </div>
  )
}
