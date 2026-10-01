import type { SiteState } from '../types'
import { ROLE } from '../labels'
import { openInbox } from '../inbox'

interface Props {
  state: SiteState | null
  actorId: string
  onActor: (id: string) => void
  lastOk: Date | null
  connected: boolean
  onReset: () => void
  resetBusy: boolean
}

export function StatusBar({ state, actorId, onActor, lastOk, connected, onReset, resetBusy }: Props) {
  const site = state?.site
  const holds = state?.holds.length ?? 0
  const actors = state?.actors ?? [{ actor_id: actorId, name: actorId, unit_id: '', roles: [] }]
  const tasksOf = (id: string) =>
    (state?.tasks ?? []).filter((t) => t.owner_actor_id === id).map((t) => t.task_id)
  return (
    <header className="statusbar">
      <span className="brand">SAFE-ORCH</span>
      <span className="stat">
        Pack <b>{site?.pack ?? '—'}</b> · {site?.site_id ?? '—'}
      </span>
      <span className="stat">
        Plan <b>R{site?.plan_revision ?? '—'}</b>
      </span>
      <span className="stat">
        Context <b>v{site?.context_version ?? '—'}</b>
      </span>
      <span className={`stat ${holds > 0 ? 'stat-hold' : ''}`}>
        ACTIVE Hold <b>{holds}</b>
      </span>
      <span className={`stat ${openInbox(state) > 0 ? 'stat-inbox' : ''}`} title="현재 Actor에게 온 답변 대기 질문">
        받은 요청 <b>{openInbox(state)}</b>
      </span>
      <span className={`stat ${state && state.dispatch.failed > 0 ? 'stat-bad' : ''}`}>
        작업 대기 {state?.dispatch.pending ?? '—'} / 실패 {state?.dispatch.failed ?? '—'}
      </span>
      <label className="actor-select">
        Actor
        <select value={actorId} onChange={(e) => onActor(e.target.value)}>
          {actors.map((a) => {
            const roles = a.roles.map((r) => ROLE[r] ?? r)
            const owns = tasksOf(a.actor_id)
            const tail = [
              roles.length ? roles.join('·') : '역할 없음',
              owns.length ? `${owns.join('·')} 담당` : '',
            ]
              .filter(Boolean)
              .join(', ')
            return (
              <option key={a.actor_id} value={a.actor_id}>
                {a.name} ({tail})
              </option>
            )
          })}
        </select>
      </label>
      <span className={`stat ${connected ? '' : 'stat-bad'}`}>
        {connected
          ? '조회 중'
          : `서버 연결 끊김${lastOk ? `, 마지막 성공 ${lastOk.toLocaleTimeString('ko-KR', { hour12: false })}` : ''}`}
      </span>
      <span className="spacer" />
      <button className="btn-danger" onClick={onReset} disabled={resetBusy}>
        시연 초기화
      </button>
    </header>
  )
}
