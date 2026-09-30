// SAFE-ORCH 최소 UI (§13, 부록 A.19). 1초 폴링, 명령 직후 즉시 재조회.

import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchState, postCommand } from './api'
import type { CandidateView, CommandOutcome, SiteState } from './types'
import { StatusBar } from './components/StatusBar'
import { Timeline } from './components/Timeline'
import { ReviewPanel } from './components/ReviewPanel'
import { Activity } from './components/Activity'
import { InputPanel } from './components/InputPanel'

const POLL_MS = 1000
const FAILS_BEFORE_OFFLINE = 2

function initialActor(): string {
  return new URLSearchParams(window.location.search).get('actor') ?? 'supervisor'
}

export default function App() {
  const [actorId, setActorId] = useState(initialActor)
  const [state, setState] = useState<SiteState | null>(null)
  const [lastOk, setLastOk] = useState<Date | null>(null)
  const [fails, setFails] = useState(0)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [pinnedRun, setPinnedRun] = useState<string | null>(null)
  const [overlay, setOverlay] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<CommandOutcome | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)

  // 후보가 candidates 목록에서 빠져도 마지막으로 받은 내용을 보여 주기 위한 캐시
  const [cache, setCache] = useState(() => new Map<string, CandidateView>())
  const seq = useRef(0)
  const actorRef = useRef(actorId) // 폴링·명령이 쓰는 현재 Actor. changeActor만 바꾼다.

  const refresh = useCallback(async () => {
    const mine = ++seq.current
    try {
      const s = await fetchState(actorRef.current)
      if (mine !== seq.current) return // 늦게 온 옛 응답은 버린다
      setCache((prev) => new Map([...prev, ...s.candidates.map((c) => [c.candidate_id, c] as const)]))
      // 선택이 없을 때만 검토 대기 첫 후보를 고른다. 한 번 고른 후보는 상태가 바뀌어도 유지한다 (A.19).
      setSelectedId((cur) => cur ?? s.review_queue[0] ?? null)
      setState(s)
      setLastOk(new Date())
      setFails(0)
      setLoadError(null)
    } catch (e) {
      if (mine !== seq.current) return
      setFails((n) => n + 1)
      setLoadError(String(e))
    }
  }, [])

  useEffect(() => {
    let stopped = false
    let timer: number | undefined
    const loop = async () => {
      await refresh()
      if (!stopped) timer = window.setTimeout(loop, POLL_MS)
    }
    void loop()
    return () => {
      stopped = true
      window.clearTimeout(timer)
    }
  }, [refresh])

  const run = useCallback(
    async (label: string, path: string, body: unknown) => {
      setBusy(label)
      const actor = actorRef.current
      try {
        const response = await postCommand(path, actor, body)
        setOutcome({ label, actor_id: actor, response })
      } finally {
        setBusy(null)
        setRefreshKey((k) => k + 1)
        void refresh()
      }
    },
    [refresh],
  )

  const changeActor = (id: string) => {
    setActorId(id)
    actorRef.current = id
    setOutcome(null)
    const url = new URL(window.location.href)
    url.searchParams.set('actor', id)
    window.history.replaceState(null, '', url)
    void refresh()
  }

  const reset = async () => {
    if (!window.confirm('시연 초기화: Plan·Hold·Run을 포함한 모든 기록을 지우고 R0로 되돌립니다. 계속할까요?')) {
      return
    }
    setBusy('시연 초기화')
    const actor = actorRef.current
    try {
      const response = await postCommand('/dev/reset', actor, { confirm: 'RESET safe_orch', start: 'R0' }, null)
      if (response.status === 'APPLIED') {
        setCache(new Map())
        setSelectedId(null)
        setPinnedRun(null)
      }
      setOutcome({ label: '시연 초기화', actor_id: actor, response })
    } finally {
      setBusy(null)
      setRefreshKey((k) => k + 1)
      void refresh()
    }
  }

  const roles = state?.actors.find((a) => a.actor_id === actorId)?.roles ?? []
  const isSupervisor = roles.includes('SUPERVISOR')
  const live = new Map((state?.candidates ?? []).map((c) => [c.candidate_id, c]))
  const candidate = selectedId ? (live.get(selectedId) ?? cache.get(selectedId) ?? null) : null
  const missing = selectedId !== null && !live.has(selectedId) && candidate !== null
  const queueNotice =
    state && selectedId && !state.review_queue.includes(selectedId)
      ? (state.review_queue.find((id) => id !== selectedId) ?? null)
      : null
  const runs = state?.runs ?? []
  const selectedRunId =
    pinnedRun && runs.some((r) => r.run_id === pinnedRun) ? pinnedRun : (runs[0]?.run_id ?? null)

  return (
    <div className="app">
      <StatusBar
        state={state}
        actorId={actorId}
        onActor={changeActor}
        lastOk={lastOk}
        connected={fails < FAILS_BEFORE_OFFLINE}
        onReset={() => void reset()}
        resetBusy={busy !== null}
      />
      {!state ? (
        <main className="loading">
          <p>상태를 불러오는 중…</p>
          {loadError && <p className="bad">조회 실패: {loadError}</p>}
        </main>
      ) : (
        <main className="main">
          <div className="left">
            <Timeline state={state} candidate={candidate} overlay={overlay} onOverlay={setOverlay} />
            <div className="left-bottom">
              <Activity
                state={state}
                actorId={actorId}
                selectedRunId={selectedRunId}
                onSelectRun={setPinnedRun}
                isSupervisor={isSupervisor}
                busy={busy}
                run={run}
                refreshKey={refreshKey}
              />
              <InputPanel state={state} roles={roles} busy={busy} run={run} />
            </div>
          </div>
          <ReviewPanel
            state={state}
            candidate={candidate}
            missing={missing}
            selectedId={selectedId}
            onSelect={setSelectedId}
            queueNotice={queueNotice}
            isSupervisor={isSupervisor}
            busy={busy}
            run={run}
            outcome={outcome}
          />
        </main>
      )}
    </div>
  )
}
