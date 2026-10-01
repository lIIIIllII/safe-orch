// 타임라인 (§13, 부록 A.19·A.20 2차). 행은 구역 + 자원. 위치는 분 → x 계산으로 그린다(scale.ts).
// 현재 Plan(실선), Plan 밖 READY 작업(점선 "요청"), 선택한 후보의 변경(굵은 테두리)을 겹쳐 그린다.
// [하루 | 전체] 보기와 날짜 탭. 비근무는 회색 사선, 전체 보기의 밤은 접힌 띠다. 자동으로 날짜를 옮기지 않는다.

import { useState } from 'react'
import type { CandidateView, SiteState } from '../types'
import { CANDIDATE_KIND, CANDIDATE_STATUS, GATE, REASON, gateReason } from '../labels'
import { ruleName, useEnv, workTypeName } from '../context'
import { type View, allScale, css, dayScale, placement, ticks } from '../scale'

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

const UNIT_COLORS = 4 // unit-0 … unit-3 (CSS). Unit은 state 순서로 색을 받는다.
const LANE_PX = 18

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

/** ?view=all 또는 ?view=day&day=N(1부터). 녹화·화면 확인용으로 URL에 남긴다. */
function initialView(days: number): View {
  const q = new URLSearchParams(window.location.search)
  if (q.get('view') === 'all') return { mode: 'all' }
  const d = Number(q.get('day') ?? '1') - 1
  return { mode: 'day', day: Number.isInteger(d) && d >= 0 && d < days ? d : 0 }
}

function saveView(v: View) {
  const url = new URL(window.location.href)
  url.searchParams.set('view', v.mode)
  if (v.mode === 'day') url.searchParams.set('day', String(v.day + 1))
  else url.searchParams.delete('day')
  window.history.replaceState(null, '', url)
}

export function Timeline({ state, candidate, overlay, onOverlay }: Props) {
  const { meta, clock } = useEnv()
  const { tasks, plan, conflicts, holds, actors } = state
  const days = clock.workDays
  const [view, setViewState] = useState<View>(() => initialView(days.length))
  const setView = (v: View) => {
    setViewState(v)
    saveView(v)
  }
  const taskMap = new Map(tasks.map((t) => [t.task_id, t]))
  const actorName = new Map(actors.map((a) => [a.actor_id, a.name]))
  const unitIndex = new Map(state.units.map((u, i) => [u.unit_id, i % UNIT_COLORS]))
  const changed = new Map(
    overlay && candidate ? candidate.changes.map((c) => [c.task_id, c]) : [],
  )
  const ruleLabel = (id: string) => ruleName(meta, id) ?? REASON[id] ?? id

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

  // 날짜 탭 배지: 그날 몫의 충돌 수와 후보 변경 수 (dayIndex: 근무 시작 1시간 전부터 다음 근무일 전까지)
  const badge = days.map((d) => ({
    conflicts: conflicts.filter((c) => clock.dayIndex(c.interval[0]) === d.index).length,
    changes: overlay && candidate
      ? candidate.changes.filter(
          (c) => clock.dayIndex(c.after.start) === d.index || clock.dayIndex(c.before.start) === d.index,
        ).length
      : 0,
  }))

  const scale = view.mode === 'day' ? dayScale(days[view.day]) : allScale(days)
  const tickList = ticks(scale, view, clock)
  const offSegs = scale.segs.filter((s) => s.off)
  const siteHold = holds.find((h) => h.scope === 'SITE')

  const background = (
    <>
      {offSegs.map((s) => {
        const box = scale.box(s.lo, s.hi)
        return box ? (
          <div
            key={`off:${s.lo}`}
            className={`tl-off ${s.fixedPx !== null ? 'tl-night' : ''}`}
            style={{ left: box.left, width: box.width }}
            title={`비근무 ${clock.format(s.lo)} → ${clock.format(s.hi)}`}
          />
        ) : null
      })}
      {tickList.map((t) => (
        <div
          key={`g:${t.m}`}
          className={`tl-grid ${t.label ? 'tl-grid-major' : ''}`}
          style={{ left: css(scale.x(t.m)) }}
        />
      ))}
    </>
  )

  return (
    <section className="panel timeline">
      <div className="panel-head">
        <h2>타임라인</h2>
        <div className="seg">
          <button
            className={view.mode === 'day' ? 'seg-on' : ''}
            onClick={() => setView({ mode: 'day', day: view.mode === 'day' ? view.day : 0 })}
          >
            하루
          </button>
          <button className={view.mode === 'all' ? 'seg-on' : ''} onClick={() => setView({ mode: 'all' })}>
            전체 {days.length}일
          </button>
        </div>
        <div className="day-tabs">
          {days.map((d) => (
            <button
              key={d.key}
              className={view.mode === 'day' && view.day === d.index ? 'day-on' : ''}
              onClick={() => setView({ mode: 'day', day: d.index })}
              title={`${d.label} 근무 ${clock.hm(d.lo)}–${clock.hm(d.hi)}`}
            >
              {d.label}
              {badge[d.index].conflicts > 0 && (
                <span className="day-badge day-badge-conflict" title="충돌">
                  {badge[d.index].conflicts}
                </span>
              )}
              {badge[d.index].changes > 0 && (
                <span className="day-badge day-badge-change" title="후보 변경">
                  {badge[d.index].changes}
                </span>
              )}
            </button>
          ))}
        </div>
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
          <span className="lg lg-off">비근무</span>
        </div>
      </div>
      {siteHold && (
        <div className="hold-banner">
          현장 Hold 중 — 신고: “{siteHold.text}” ({actorName.get(siteHold.reporter_actor_id) ?? siteHold.reporter_actor_id})
        </div>
      )}
      <div className={`tl ${siteHold ? 'tl-site-hold' : ''}`}>
        <div className="tl-row tl-axis">
          <div className="tl-label tl-day-label">
            {view.mode === 'day' ? days[view.day].label : ''}
          </div>
          <div className="tl-track tl-axis-track">
            {view.mode === 'all' &&
              days.map((d) => (
                <span key={`d:${d.key}`} className="tl-day" style={{ left: css(scale.x(d.lo)) }}>
                  {d.label}
                </span>
              ))}
            {tickList
              .filter((t) => t.label)
              .map((t) => (
                <span key={`t:${t.m}`} className="tl-tick" style={{ left: css(scale.x(t.m)) }}>
                  {t.label}
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
              <div className="tl-track" style={{ height: laneCount * LANE_PX + 4 }}>
                {background}
                {rowConflicts.map((c) => {
                  const box = scale.box(c.interval[0], c.interval[1])
                  if (!box) return null
                  return (
                    <div
                      key={`${c.rule_id}:${c.task_ids.join(',')}`}
                      className="tl-conflict"
                      style={{ left: box.left, width: box.width }}
                      title={`${ruleLabel(c.rule_id)} (${c.rule_id}) · ${c.task_ids.join(', ')} · ${clock.span(c.interval[0], c.interval[1])}`}
                    >
                      <span>{ruleLabel(c.rule_id)}</span>
                    </div>
                  )
                })}
                {rowBars.map((b) => {
                  const where = placement(scale, view, clock, b.start, b.end)
                  if (!where) return null
                  const t = taskMap.get(b.taskId)
                  const unit = `unit-${unitIndex.get(t?.unit_id ?? '') ?? UNIT_COLORS - 1}`
                  const held = t?.gate === 'HOLD'
                  const night = !clock.inWork(b.start, b.end)
                  // 전체 보기는 막대가 좁아 Gate 배지가 작업 ID를 가린다. 그때는 제목(title)에만 둔다.
                  const showGate =
                    view.mode === 'day' && (b.kind === 'plan' || b.kind === 'before') && t && t.gate !== 'ALLOW'
                  const wt = t ? workTypeName(meta, t.work_type) : ''
                  const title = [
                    `${b.taskId} ${wt}`,
                    clock.span(b.start, b.end),
                    night ? '근무시간 밖' : '',
                    b.resourceId ?? '자원 없음',
                    `담당 ${actorName.get(t?.owner_actor_id ?? '') ?? t?.owner_actor_id ?? ''}`,
                    t ? `Gate ${GATE[t.gate]}: ${t.reasons.map(gateReason).join(', ') || '—'}` : '',
                  ]
                    .filter(Boolean)
                    .join('\n')
                  return (
                    <div
                      key={b.key}
                      className={[
                        'bar',
                        `bar-${b.kind}`,
                        unit,
                        held ? 'bar-held' : '',
                        night ? 'bar-night' : '',
                        where.clipL ? 'bar-clip-l' : '',
                        where.clipR ? 'bar-clip-r' : '',
                      ].join(' ')}
                      style={{
                        left: where.left,
                        width: where.width,
                        top: 2 + (laneOf.get(b.key) ?? 0) * LANE_PX,
                        height: LANE_PX - 2,
                      }}
                      title={title}
                    >
                      {where.edge ? (
                        <span className="bar-text">{where.edge === 'left' ? '◀' : '▶'}</span>
                      ) : (
                        <span className="bar-text">
                          {b.kind === 'after' && '→ '}
                          {b.taskId} {wt}
                          {b.kind === 'request' && ' · 요청'}
                          {b.kind === 'after' && b.resourceId && row.group === 'zone' && ` · ${b.resourceId}`}
                        </span>
                      )}
                      {showGate && !where.edge && (
                        <span className={`gate gate-${t.gate.toLowerCase()}`}>{GATE[t.gate]}</span>
                      )}
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
