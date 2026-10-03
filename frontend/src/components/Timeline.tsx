// 타임라인. 행은 구역 + 자원. 위치는 분 → x(px)로 그린다(scale.ts).
// 현재 Plan(실선), Plan 밖 READY 작업(점선 "요청"), 선택한 후보의 변경(굵은 테두리)을 겹쳐 그린다.
// [하루 | 전체] 보기와 날짜 탭. 분당 픽셀로 그려 넘치면 가로 스크롤(시간 머리줄·행 이름 고정). 비근무는 회색 사선,
// 전체 보기의 밤은 접힌 띠다. 자동으로 날짜를 옮기지 않고, 가로 자동 스크롤은 사용자가 직접 스크롤하기 전까지만 한다.

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { CandidateView, Conflict, SiteState, Task } from '../types'
import { CANDIDATE_KIND, CANDIDATE_STATUS, GATE, REASON, gateReason } from '../labels'
import { ruleName, useEnv, workTypeName } from '../context'
import {
  ALL_ZOOM_DEFAULT,
  DAY_ZOOMS,
  DAY_ZOOM_DEFAULT,
  type View,
  allScale,
  dayScale,
  placement,
  ticks,
} from '../scale'

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
  /** 타임라인을 화면 전체 폭으로 ("크게 보기") */
  wide: boolean
  onWide: (on: boolean) => void
}

const UNIT_COLORS = 4 // unit-0 … unit-3 (CSS). Unit은 state 순서로 색을 받는다.
// 행·막대 크기: 행 높이 36px, 행 이름 14px, 막대 2줄(작업 ID·유형 / 시각 범위)
const LABEL_W = 128
const LANE_PX = 36
const BAR_H = 32
const BAR_PAD = 12 // 막대 안쪽 여백 + 테두리
const OUT_GAP = 4 // 막대 바깥 라벨과 막대 사이
const AXIS_H = 34
const AUTO_MARGIN_PX = 80 // 자동 스크롤 시 대상 왼쪽 여백
const MIN_PANEL_PX = 160
const CONFLICT_STRIP_PX = 18 // 충돌이 있는 행 위쪽의 충돌 이름 띠(막대 라벨과 겹치지 않게)

/** 라벨 폭 측정(막대 안에 들어가는지 판단). 화면 글꼴로 canvas에서 잰다. */
let measure: { ctx: CanvasRenderingContext2D; family: string } | null = null
function textWidth(text: string, size: number, bold: boolean): number {
  if (!measure) {
    const ctx = document.createElement('canvas').getContext('2d')
    if (!ctx) return text.length * size
    measure = { ctx, family: getComputedStyle(document.body).fontFamily }
  }
  measure.ctx.font = `${bold ? 700 : 400} ${size}px ${measure.family}`
  return measure.ctx.measureText(text).width
}

/** 막대(바깥 라벨 포함) 가로 범위로 겹치지 않게 줄(lane)을 나눈다. */
function lanes(items: { key: string; lo: number; hi: number }[]): Map<string, number> {
  const out = new Map<string, number>()
  const ends: number[] = []
  for (const b of [...items].sort((x, y) => x.lo - y.lo || x.hi - y.hi)) {
    let lane = ends.findIndex((e) => e + 2 <= b.lo)
    if (lane < 0) {
      lane = ends.length
      ends.push(b.hi)
    } else {
      ends[lane] = b.hi
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

type Zoom = number | 'fit'

const KIND_LABEL: Record<BarKind, string> = {
  plan: '현재 Plan',
  request: '요청(Plan 밖)',
  before: '후보 변경 전',
  after: '후보 변경 후',
}

export function Timeline({ state, candidate, overlay, onOverlay, wide, onWide }: Props) {
  const { meta, clock } = useEnv()
  const { tasks, plan, conflicts, holds, actors } = state
  const days = clock.workDays
  const [view, setViewState] = useState<View>(() => initialView(days.length))
  const [zoom, setZoom] = useState<{ day: Zoom; all: Zoom }>({
    day: DAY_ZOOM_DEFAULT,
    all: ALL_ZOOM_DEFAULT,
  })
  const [busyOnly, setBusyOnly] = useState(true)
  const [height, setHeight] = useState<number | null>(null)
  const [hover, setHover] = useState<{ key: string; x: number; y: number } | null>(null)
  const [avail, setAvail] = useState(0)
  // 가로 스크롤 위치: 행 이름 열 뒤로 들어간 막대의 라벨을 보이는 쪽으로 민다
  const [scrollX, setScrollX] = useState(0)
  const frame = useRef<number | null>(null)
  const scroller = useRef<HTMLDivElement>(null)
  // 사용자가 직접 가로 스크롤했으면 자동 스크롤하지 않는다(녹화 중 화면이 튀지 않게). 보기·배율을 바꾸면 초기화.
  const userScrolled = useRef(false)
  const expectedLeft = useRef<number | null>(null)
  const lastLeft = useRef(0)

  const setView = (v: View) => {
    userScrolled.current = false
    setViewState(v)
    saveView(v)
  }
  const z = view.mode === 'day' ? zoom.day : zoom.all
  const setZ = (v: Zoom) => {
    userScrolled.current = false
    setZoom(view.mode === 'day' ? { ...zoom, day: v } : { ...zoom, all: v })
  }

  // "근무시간 맞춤"에 쓸 패널 폭
  useEffect(() => {
    const el = scroller.current
    if (!el) return
    const ro = new ResizeObserver(() => setAvail(Math.max(0, el.clientWidth - LABEL_W)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const taskMap = new Map(tasks.map((t) => [t.task_id, t]))
  const actorName = new Map(actors.map((a) => [a.actor_id, a.name]))
  const unitName = new Map(state.units.map((u) => [u.unit_id, u.name]))
  const unitIndex = new Map(state.units.map((u, i) => [u.unit_id, i % UNIT_COLORS]))
  const changed = new Map(overlay && candidate ? candidate.changes.map((c) => [c.task_id, c]) : [])
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

  const allRows: { id: string; label: string; group: 'zone' | 'resource' }[] = [
    ...state.zones.map((zn) => ({ id: zn, label: `${zn} 구역`, group: 'zone' as const })),
    ...state.resources.map((r) => ({
      id: r.resource_id,
      label: r.resource_id,
      group: 'resource' as const,
    })),
  ]

  // 날짜 탭 배지: 그날 몫의 충돌 수와 후보 변경 수 (dayIndex: 근무 시작 1시간 전부터 다음 근무일 전까지)
  const badge = days.map((d) => ({
    conflicts: conflicts.filter((c) => clock.dayIndex(c.interval[0]) === d.index).length,
    changes:
      overlay && candidate
        ? candidate.changes.filter(
            (c) => clock.dayIndex(c.after.start) === d.index || clock.dayIndex(c.before.start) === d.index,
          ).length
        : 0,
  }))

  const fitPx = z === 'fit' && avail > 0 ? avail : null
  const pxPerMin = z === 'fit' ? 1 : z
  const scale = view.mode === 'day' ? dayScale(days[view.day], pxPerMin, fitPx) : allScale(days, pxPerMin, fitPx)
  const tickList = ticks(scale, view, clock)
  const offSegs = scale.segs.filter((s) => s.off)
  const siteHold = holds.find((h) => h.scope === 'SITE')

  // 막대마다 위치·라벨·바깥 라벨 여부. 라벨 = 작업 ID·유형 / 시각 범위(2줄). 좁으면 막대 오른쪽 바깥(끝이면 왼쪽).
  const drawn = bars.flatMap((b) => {
    const where = placement(scale, view, clock, b.start, b.end)
    if (!where) return []
    const t = taskMap.get(b.taskId)
    const wt = t ? workTypeName(meta, t.work_type) : ''
    const line1 = `${b.kind === 'after' ? '→ ' : ''}${b.taskId} ${wt}${b.kind === 'request' ? ' · 요청' : ''}`
    const line2 = `${clock.hm(b.start)}–${clock.hm(b.end)}`
    // Gate(시작 가능 아님)는 막대 안에 글자가 들어갈 때만 배지로, 아니면 모서리 표시로 둔다(사유는 카드)
    const gated = (b.kind === 'plan' || b.kind === 'before') && !!t && t.gate !== 'ALLOW'
    const textW = Math.max(textWidth(line1, 14, true), textWidth(line2, 12, false))
    const inside = where.edge !== null || where.width >= textW + BAR_PAD
    const gateW = gated && t ? textWidth(GATE[t.gate], 11, false) + 14 : 0
    const showGate = gated && inside && where.edge === null && where.width >= textWidth(line1, 14, true) + gateW + BAR_PAD
    let outLeft: number | null = null
    if (!inside) {
      const right = where.left + where.width + OUT_GAP
      outLeft = right + textW + 8 <= scale.width ? right : Math.max(0, where.left - OUT_GAP - textW - 8)
    }
    const lo = outLeft !== null ? Math.min(where.left, outLeft) : where.left
    const hi = outLeft !== null ? Math.max(where.left + where.width, outLeft + textW + 8) : where.left + where.width
    return [{ b, t, wt, where, line1, line2, gated, showGate, inside, textW, outLeft, lo, hi }]
  })
  const drawnBy = new Map(drawn.map((d) => [d.b.key, d]))

  const rowData = allRows.map((row) => {
    const rowBars = drawn.filter((d) => (row.group === 'zone' ? d.b.zoneId === row.id : d.b.resourceId === row.id))
    const rowConflicts = conflicts.filter(
      (c) =>
        (row.group === 'zone' ? c.zone_ids.includes(row.id) : c.resource_id === row.id) &&
        scale.box(c.interval[0], c.interval[1]) !== null,
    )
    const laneOf = lanes(rowBars.map((d) => ({ key: d.b.key, lo: d.lo, hi: d.hi })))
    const laneCount = Math.max(1, ...[...laneOf.values()].map((l) => l + 1))
    return { row, rowBars, rowConflicts, laneOf, laneCount }
  })
  // "작업 있는 행만": 이 보기 범위에 막대·충돌(후보 포함)이 없는 행을 숨긴다
  const rows = busyOnly ? rowData.filter((r) => r.rowBars.length > 0 || r.rowConflicts.length > 0) : rowData

  // 자동 스크롤 대상: 이 보기의 충돌 시작, 없으면 후보 변경(전·후) 시작 중 가장 이른 것
  const inView = (m: number) => m >= scale.start && m < scale.end
  const conflictStarts = conflicts
    .filter((c) => scale.box(c.interval[0], c.interval[1]) !== null)
    .map((c) => Math.max(c.interval[0], scale.start))
  const changeStarts =
    overlay && candidate
      ? candidate.changes.flatMap((c) => [c.after.start, c.before.start]).filter(inView)
      : []
  const target = conflictStarts.length ? Math.min(...conflictStarts) : changeStarts.length ? Math.min(...changeStarts) : null
  const autoKey = [
    view.mode,
    view.mode === 'day' ? view.day : '',
    String(z),
    conflicts.map((c) => `${c.rule_id}:${c.task_ids.join(',')}:${c.interval.join('-')}`).join('|'),
    overlay && candidate ? candidate.candidate_id : '',
  ].join('#')

  const scrollToMinute = (m: number | null) => {
    const el = scroller.current
    if (!el || m === null) return
    el.scrollLeft = Math.max(0, scale.x(m) - AUTO_MARGIN_PX)
    expectedLeft.current = el.scrollLeft
    lastLeft.current = el.scrollLeft
    setScrollX(el.scrollLeft)
  }

  useLayoutEffect(() => {
    if (!userScrolled.current) scrollToMinute(target)
    // autoKey가 바뀔 때(처음, 보기·배율, 충돌·후보 변경)와 패널 폭이 정해질 때만 자동 스크롤한다
  }, [autoKey, avail]) // eslint-disable-line react-hooks/exhaustive-deps

  const onScroll = () => {
    const el = scroller.current
    if (!el) return
    if (frame.current === null) {
      frame.current = requestAnimationFrame(() => {
        frame.current = null
        setScrollX(scroller.current?.scrollLeft ?? 0)
      })
    }
    if (el.scrollLeft === lastLeft.current) return
    if (expectedLeft.current === null || Math.abs(el.scrollLeft - expectedLeft.current) > 1) {
      userScrolled.current = true
    }
    lastLeft.current = el.scrollLeft
  }

  /** "충돌로 이동": 이 보기에 충돌이 없으면 충돌이 있는 첫 날로 옮긴다. */
  const gotoConflict = () => {
    if (conflictStarts.length) {
      scrollToMinute(Math.min(...conflictStarts))
      return
    }
    const first = [...conflicts].sort((a, b) => a.interval[0] - b.interval[0])[0]
    if (!first) return
    if (view.mode === 'day') setView({ mode: 'day', day: clock.dayIndex(first.interval[0]) })
    else scrollToMinute(first.interval[0])
  }

  // 패널 높이 끌기 (아래 손잡이)
  const startDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    const panel = e.currentTarget.parentElement
    if (!panel) return
    const y0 = e.clientY
    const h0 = panel.getBoundingClientRect().height
    const move = (ev: PointerEvent) =>
      setHeight(Math.min(window.innerHeight - MIN_PANEL_PX, Math.max(MIN_PANEL_PX, h0 + ev.clientY - y0)))
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

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
        <div key={`g:${t.m}`} className={`tl-grid ${t.label ? 'tl-grid-major' : ''}`} style={{ left: scale.x(t.m) }} />
      ))}
    </>
  )

  const hovered = hover ? drawnBy.get(hover.key) : undefined
  const enter = (key: string) => (e: React.MouseEvent) => setHover({ key, x: e.clientX, y: e.clientY })
  const leave = () => setHover(null)

  return (
    <section
      className={`panel timeline ${wide ? 'timeline-wide' : ''}`}
      style={height !== null ? { height, maxHeight: 'none' } : undefined}
    >
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
        <div className="seg" title="분당 픽셀">
          {(view.mode === 'day' ? DAY_ZOOMS : [ALL_ZOOM_DEFAULT]).map((v) => (
            <button key={v} className={z === v ? 'seg-on' : ''} onClick={() => setZ(v)}>
              {v}px/분
            </button>
          ))}
          <button className={z === 'fit' ? 'seg-on' : ''} onClick={() => setZ('fit')}>
            근무시간 맞춤
          </button>
        </div>
        <button className="btn-small" onClick={gotoConflict} disabled={conflicts.length === 0}>
          충돌로 이동
        </button>
        <label className="toggle">
          <input type="checkbox" checked={busyOnly} onChange={(e) => setBusyOnly(e.target.checked)} />
          작업 있는 행만
        </label>
        <label className="toggle">
          <input type="checkbox" checked={overlay} onChange={(e) => onOverlay(e.target.checked)} />
          후보 겹쳐 보기
        </label>
        <button className="btn-small" onClick={() => onWide(!wide)}>
          {wide ? '기본 배치' : '크게 보기'}
        </button>
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
          <span className="lg lg-gate" title="재확정 필요·보류. 사유는 막대에 마우스를 올리면 보인다">Gate</span>
        </div>
      </div>
      {siteHold && (
        <div className="hold-banner">
          현장 Hold 중 — 신고: “{siteHold.text}” ({actorName.get(siteHold.reporter_actor_id) ?? siteHold.reporter_actor_id})
        </div>
      )}
      <div className={`tl ${siteHold ? 'tl-site-hold' : ''}`} ref={scroller} onScroll={onScroll}>
        <div className="tl-canvas" style={{ width: LABEL_W + scale.width }}>
          <div className="tl-row tl-axis">
            <div className="tl-label tl-day-label" style={{ width: LABEL_W }}>
              {view.mode === 'day' ? days[view.day].label : `${days.length}일`}
            </div>
            <div className="tl-track tl-axis-track" style={{ width: scale.width, height: AXIS_H }}>
              {view.mode === 'all' &&
                days.map((d) => (
                  <span key={`d:${d.key}`} className="tl-day" style={{ left: scale.x(d.lo) }}>
                    {d.label}
                  </span>
                ))}
              {tickList
                .filter((t) => t.label)
                .map((t) => (
                  <span
                    key={`t:${t.m}`}
                    className="tl-tick"
                    // 가장자리 눈금은 가운데 정렬하면 반이 잘리므로 안쪽으로 붙인다
                    style={{
                      left: scale.x(t.m),
                      transform:
                        scale.x(t.m) < 24 ? 'none' : scale.x(t.m) > scale.width - 24 ? 'translateX(-100%)' : undefined,
                    }}
                  >
                    {t.label}
                  </span>
                ))}
            </div>
          </div>
          {rows.length === 0 && <p className="muted tl-empty">이 보기 범위에 작업이 없습니다.</p>}
          {rows.map(({ row, rowBars, rowConflicts, laneOf, laneCount }, i) => {
            const sep = i > 0 && row.group === 'resource' && rows[i - 1].row.group === 'zone'
            const strip = rowConflicts.length > 0 ? CONFLICT_STRIP_PX : 0
            return (
              <div key={`${row.group}:${row.id}`} className={`tl-row ${sep ? 'tl-sep' : ''}`}>
                <div className="tl-label" style={{ width: LABEL_W }}>
                  {row.label}
                </div>
                <div className="tl-track" style={{ width: scale.width, height: strip + laneCount * LANE_PX }}>
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
                  {rowBars.map((d) => {
                    const { b, t, where } = d
                    const unit = `unit-${unitIndex.get(t?.unit_id ?? '') ?? UNIT_COLORS - 1}`
                    const night = !clock.inWork(b.start, b.end)
                    const top = strip + 2 + (laneOf.get(b.key) ?? 0) * LANE_PX
                    const label = (
                      <>
                        <span className="bar-line1">
                          {d.line1}
                          {d.showGate && t && (
                            <span className={`gate gate-${t.gate.toLowerCase()}`}>{GATE[t.gate]}</span>
                          )}
                        </span>
                        <span className="bar-line2">{d.line2}</span>
                      </>
                    )
                    return (
                      <div key={b.key}>
                        <div
                          className={[
                            'bar',
                            `bar-${b.kind}`,
                            unit,
                            t?.gate === 'HOLD' ? 'bar-held' : '',
                            d.gated && !d.showGate && t ? `bar-gate bar-gate-${t.gate.toLowerCase()}` : '',
                            night ? 'bar-night' : '',
                            where.clipL ? 'bar-clip-l' : '',
                            where.clipR ? 'bar-clip-r' : '',
                          ].join(' ')}
                          style={{
                            left: where.left,
                            width: where.width,
                            top,
                            height: BAR_H,
                            // 막대 왼쪽이 스크롤로 가려지면 라벨을 보이는 곳까지 민다(막대 안에서만)
                            paddingLeft:
                              5 +
                              Math.max(0, Math.min(scrollX - where.left, where.width - d.textW - BAR_PAD)),
                          }}
                          onMouseEnter={enter(b.key)}
                          onMouseMove={enter(b.key)}
                          onMouseLeave={leave}
                        >
                          {where.edge ? (
                            <span className="bar-line1">{where.edge === 'left' ? '◀' : '▶'}</span>
                          ) : (
                            d.inside && label
                          )}
                        </div>
                        {d.outLeft !== null && (
                          <div
                            className={`bar-out bar-out-${b.kind}`}
                            style={{ left: d.outLeft, top, height: BAR_H }}
                            onMouseEnter={enter(b.key)}
                            onMouseMove={enter(b.key)}
                            onMouseLeave={leave}
                          >
                            {label}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>
      </div>
      <div className="tl-resize" onPointerDown={startDrag} title="끌어서 타임라인 높이 조절" />
      {hovered && hover && (
        <BarCard
          d={hovered}
          x={hover.x}
          y={hover.y}
          owner={actorName.get(hovered.t?.owner_actor_id ?? '') ?? hovered.t?.owner_actor_id ?? '—'}
          unit={unitName.get(hovered.t?.unit_id ?? '') ?? hovered.t?.unit_id ?? '—'}
          conflicts={conflicts.filter((c) => c.task_ids.includes(hovered.b.taskId))}
          ruleLabel={ruleLabel}
        />
      )}
    </section>
  )
}

/** 마우스를 올리면 나오는 카드: 작업·담당·시각·자원·Gate·충돌. 화면 밖으로 나가지 않게 뒤집는다. */
function BarCard({
  d,
  x,
  y,
  owner,
  unit,
  conflicts,
  ruleLabel,
}: {
  d: { b: Bar; t: Task | undefined; wt: string }
  x: number
  y: number
  owner: string
  unit: string
  conflicts: Conflict[]
  ruleLabel: (id: string) => string
}) {
  const { clock } = useEnv()
  const { b, t } = d
  const CARD_W = 320
  const left = x + 14 + CARD_W > window.innerWidth ? x - 14 - CARD_W : x + 14
  const top = Math.min(y + 14, window.innerHeight - 220)
  return (
    <div className="tl-card" style={{ left, top, width: CARD_W }}>
      <div className="tl-card-head">
        <b>
          {b.taskId} {d.wt}
        </b>
        <span className="muted small">{KIND_LABEL[b.kind]}</span>
      </div>
      <table className="tbl small">
        <tbody>
          <tr>
            <th>담당</th>
            <td>
              {owner} · {unit}
            </td>
          </tr>
          <tr>
            <th>시각</th>
            <td>
              {clock.span(b.start, b.end)}
              {!clock.inWork(b.start, b.end) && <span className="bad"> · 근무시간 밖</span>}
            </td>
          </tr>
          <tr>
            <th>구역·자원</th>
            <td>
              {b.zoneId} 구역 · {b.resourceId ?? '자원 없음'}
            </td>
          </tr>
          <tr>
            <th>Gate</th>
            <td>
              {t ? `${GATE[t.gate]} — ${t.reasons.map(gateReason).join(', ') || '사유 없음'}` : '—'}
            </td>
          </tr>
          <tr>
            <th>충돌</th>
            <td>
              {conflicts.length === 0
                ? '없음'
                : conflicts.map((c) => (
                    <div key={`${c.rule_id}:${c.task_ids.join(',')}`}>
                      {ruleLabel(c.rule_id)} · {c.task_ids.join(', ')} · {clock.span(c.interval[0], c.interval[1])}
                    </div>
                  ))}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  )
}
