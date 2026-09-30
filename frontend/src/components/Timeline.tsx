// 타임라인 (§13, 부록 A.19). 1분 = CSS grid 1칸. 행은 구역 + 자원.
// 현재 Plan(실선), Plan 밖 READY 작업(점선 "요청"), 선택한 후보의 변경(굵은 테두리)을 겹쳐 그린다.

import type { CSSProperties } from 'react'
import type { CandidateView, SiteState } from '../types'
import { CANDIDATE_KIND, CANDIDATE_STATUS, GATE, REASON, WORK_TYPE, gateReason } from '../labels'
import { minuteToClock, span } from '../time'

type BarKind = 'plan' | 'request' | 'before' | 'after'

interface Bar {
  key: string
  taskId: string
  start: number
  end: number
  resourceId: string | null
  zoneId: string
  kind: BarKind
}

interface Props {
  state: SiteState
  candidate: CandidateView | null
  overlay: boolean
  onOverlay: (on: boolean) => void
}

const UNIT_CLASS: Record<string, string> = { UA: 'unit-a', UB: 'unit-b', SITE: 'unit-site' }

function lanes(bars: Bar[]): Map<string, number> {
  const out = new Map<string, number>()
  const ends: number[] = []
  for (const b of [...bars].sort((x, y) => x.start - y.start || x.end - y.end)) {
    let lane = ends.findIndex((e) => e <= b.start)
    if (lane < 0) {
      lane = ends.length
      ends.push(b.end)
    } else {
      ends[lane] = b.end
    }
    out.set(b.key, lane)
  }
  return out
}

export function Timeline({ state, candidate, overlay, onOverlay }: Props) {
  const { site, tasks, plan, conflicts, holds, actors } = state
  const n = site.horizon_minutes
  const origin = site.horizon_start_utc
  const taskMap = new Map(tasks.map((t) => [t.task_id, t]))
  const actorName = new Map(actors.map((a) => [a.actor_id, a.name]))
  const changed = new Map(
    overlay && candidate ? candidate.changes.map((c) => [c.task_id, c]) : [],
  )

  const bars: Bar[] = []
  const inPlan = new Set(plan.assignments.map((a) => a.task_id))
  for (const a of plan.assignments) {
    const t = taskMap.get(a.task_id)
    if (!t) continue
    bars.push({
      key: `p:${a.task_id}`,
      taskId: a.task_id,
      start: a.start,
      end: a.end,
      resourceId: a.resource_id,
      zoneId: t.zone_id,
      kind: changed.has(a.task_id) ? 'before' : 'plan',
    })
  }
  for (const t of tasks) {
    if (inPlan.has(t.task_id) || t.lifecycle !== 'READY') continue
    bars.push({
      key: `r:${t.task_id}`,
      taskId: t.task_id,
      start: t.earliest_start,
      end: t.earliest_start + t.duration,
      resourceId: t.requested_resource_id,
      zoneId: t.zone_id,
      kind: changed.has(t.task_id) ? 'before' : 'request',
    })
  }
  for (const c of changed.values()) {
    const t = taskMap.get(c.task_id)
    bars.push({
      key: `c:${c.task_id}`,
      taskId: c.task_id,
      start: c.after.start,
      end: c.after.end,
      resourceId: c.after.resource_id,
      zoneId: t?.zone_id ?? '',
      kind: 'after',
    })
  }

  const rows: { id: string; label: string; group: 'zone' | 'resource' }[] = [
    ...state.zones.map((z) => ({ id: z, label: `${z} 구역`, group: 'zone' as const })),
    ...state.resources.map((r) => ({
      id: r.resource_id,
      label: r.resource_id,
      group: 'resource' as const,
    })),
  ]

  const siteHold = holds.find((h) => h.scope === 'SITE')
  const ticks = Array.from({ length: Math.floor(n / 30) + 1 }, (_, i) => i * 30)
  const trackStyle = { gridTemplateColumns: `repeat(${n}, 1fr)`, '--tick': `${(100 * 15) / n}%` }

  return (
    <section className="panel timeline">
      <div className="panel-head">
        <h2>타임라인</h2>
        <label className="toggle">
          <input type="checkbox" checked={overlay} onChange={(e) => onOverlay(e.target.checked)} />
          후보 겹쳐 보기
        </label>
        <div className="legend">
          <span className="lg lg-plan">현재 Plan R{plan.plan_revision}</span>
          <span className="lg lg-request">요청(Plan 밖)</span>
          {overlay && candidate && (
            <span className="lg lg-after">
              후보 {candidate.candidate_id.slice(0, 12)} · {CANDIDATE_KIND[candidate.kind]} ·{' '}
              {CANDIDATE_STATUS[candidate.display_status] ?? candidate.display_status}
            </span>
          )}
          <span className="lg lg-conflict">충돌</span>
        </div>
      </div>
      {siteHold && (
        <div className="hold-banner">
          현장 Hold 중 — 신고: “{siteHold.text}” ({actorName.get(siteHold.reporter_actor_id) ?? siteHold.reporter_actor_id})
        </div>
      )}
      <div className={`tl ${siteHold ? 'tl-site-hold' : ''}`}>
        <div className="tl-row tl-axis">
          <div className="tl-label" />
          <div className="tl-track" style={trackStyle as CSSProperties}>
            {ticks.map((m) => (
              <span
                key={m}
                className="tl-tick"
                style={{ gridColumn: `${Math.min(m, n - 1) + 1} / span 1`, gridRow: 1 }}
              >
                {minuteToClock(origin, m)}
              </span>
            ))}
          </div>
        </div>
        {rows.map((row, i) => {
          const rowBars = bars.filter((b) =>
            row.group === 'zone' ? b.zoneId === row.id : b.resourceId === row.id,
          )
          const laneOf = lanes(rowBars)
          const laneCount = Math.max(1, ...[...laneOf.values()].map((l) => l + 1))
          const rowConflicts = conflicts.filter((c) =>
            row.group === 'zone' ? c.zone_ids.includes(row.id) : c.resource_id === row.id,
          )
          const sep = i > 0 && row.group === 'resource' && rows[i - 1].group === 'zone'
          return (
            <div key={`${row.group}:${row.id}`} className={`tl-row ${sep ? 'tl-sep' : ''}`}>
              <div className="tl-label">{row.label}</div>
              <div
                className="tl-track"
                style={
                  {
                    ...trackStyle,
                    gridTemplateRows: `repeat(${laneCount}, 26px)`,
                  } as CSSProperties
                }
              >
                {rowConflicts.map((c) => (
                  <div
                    key={`${c.rule_id}:${c.task_ids.join(',')}`}
                    className="tl-conflict"
                    style={{ gridColumn: `${c.interval[0] + 1} / ${c.interval[1] + 1}`, gridRow: '1 / -1' }}
                    title={`${REASON[c.rule_id] ?? c.rule_id} (${c.rule_id}) · ${c.task_ids.join(', ')} · ${span(origin, c.interval[0], c.interval[1])}`}
                  >
                    <span>{REASON[c.rule_id] ?? c.rule_id}</span>
                  </div>
                ))}
                {rowBars.map((b) => {
                  const t = taskMap.get(b.taskId)
                  const unit = UNIT_CLASS[t?.unit_id ?? ''] ?? 'unit-other'
                  const held = t?.gate === 'HOLD'
                  const showGate = (b.kind === 'plan' || b.kind === 'before') && t && t.gate !== 'ALLOW'
                  const title = [
                    `${b.taskId} ${WORK_TYPE[t?.work_type ?? ''] ?? t?.work_type ?? ''}`,
                    span(origin, b.start, b.end),
                    b.resourceId ?? '자원 없음',
                    `담당 ${actorName.get(t?.owner_actor_id ?? '') ?? t?.owner_actor_id ?? ''}`,
                    t ? `Gate ${GATE[t.gate]}: ${t.reasons.map(gateReason).join(', ') || '—'}` : '',
                  ]
                    .filter(Boolean)
                    .join('\n')
                  return (
                    <div
                      key={b.key}
                      className={`bar bar-${b.kind} ${unit} ${held ? 'bar-held' : ''}`}
                      style={{
                        gridColumn: `${b.start + 1} / ${b.end + 1}`,
                        gridRow: (laneOf.get(b.key) ?? 0) + 1,
                      }}
                      title={title}
                    >
                      <span className="bar-text">
                        {b.kind === 'after' && '→ '}
                        {b.taskId} {WORK_TYPE[t?.work_type ?? ''] ?? ''}
                        {b.kind === 'request' && ' · 요청'}
                        {b.kind === 'after' && b.resourceId && row.group === 'zone' && ` · ${b.resourceId}`}
                      </span>
                      {showGate && <span className={`gate gate-${t.gate.toLowerCase()}`}>{GATE[t.gate]}</span>}
                    </div>
                  )
                })}
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}
