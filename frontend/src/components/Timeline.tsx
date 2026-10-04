// 타임라인. 행은 구역 + 자원. 위치는 분 → x(px)로 그린다(scale.ts).
// 현재 Plan(실선), Plan 밖 READY 작업(점선 "요청"), 선택한 후보의 변경(굵은 테두리)을 겹쳐 그린다.
// 막대를 누르면 작업이 선택되고 카드가 고정되어 열린다: 고정·고정 해제, 희망 영역 그리기·지우기(AG-27).
// 고정은 자물쇠와 굵은 테두리, 희망 영역은 막대 뒤 Unit 색의 옅은 띠(늘 보임, 접수 Agent가 정했고 아직 확인하지
// 않은 희망은 점선 테두리), 가능 범위(시간창)는 막대 아래 가는 괄호다. 괄호는 사람이 좁힌 작업은 늘 보이고,
// Horizon 전체인 작업은 선택했을 때만 보인다. 후보 겹쳐 보기에서는 후보가 옮긴 자리에도 둘을 같이 그린다.
// 담당자는 자기 작업의 Plan 막대를 끌어 시각을 옮긴다(AG-31): 놓을 수 있는 구간은 서버가 계산해 주고 화면은 칠하기만
// 한다. 놓으면 미리보기와 [확정]/[취소]가 뜬다. 조금만 움직이면 선택이다. 희망 영역은 [희망 영역 그리기]를 누른 뒤
// 그 작업의 행에서 끈다. 작업 카드의 [작업 없애기]는 서버 확인을 거쳐 [확정]해야 없어진다(계획 밖 요청은 요청 철회).
// [하루 | 전체] 보기와 날짜 탭. 분당 픽셀로 그려 넘치면 가로 스크롤(시간 머리줄·행 이름 고정). 비근무는 회색 사선,
// 전체 보기의 밤은 접힌 띠다. 자동으로 날짜를 옮기지 않고, 가로 자동 스크롤은 사용자가 직접 스크롤하기 전까지만 한다.

import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { fetchMoveCheck, fetchMoveRange, fetchRemoveCheck } from '../api'
import type { CandidateView, Conflict, MoveCheck, MoveOptions, RemoveCheck, SiteState, Task } from '../types'
import { CANDIDATE_KIND, CANDIDATE_STATUS, GATE, REASON, VALUE_NAME, gateReason } from '../labels'
import { decidedValues, poolExcessText, ruleName, useEnv, workTypeName } from '../context'
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
import type { Run } from './ReviewPanel'
import { TaskEdit } from './TaskEdit'

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
  /** 지금 Actor. 고정·희망 영역 버튼의 권한 안내에 쓴다(판정은 서버가 한다) */
  actorId: string
  isSupervisor: boolean
  busy: string | null
  run: Run
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
const DRAW_SNAP_MIN = 5 // 희망 영역을 그릴 때 맞추는 분 단위
const DRAG_START_PX = 6 // 이만큼 끌어야 이동이다. 덜 움직이면 선택
const PIN_MARK = '🔒'

/** 직접 이동의 미리보기. 놓을 수 있는 구간(options)과 놓은 자리의 판정(check)은 서버가 준다. */
interface Move {
  taskId: string
  origin: number
  duration: number
  start: number
  dropped: boolean
  x: number
  y: number
  options: MoveOptions | null
  check: MoveCheck | null
  error: string | null
}

const inRanges = (o: MoveOptions, start: number) => o.ranges.some((r) => r.start_min <= start && start <= r.start_max)

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

export function Timeline(props: Props) {
  const { state, candidate, overlay, onOverlay, wide, onWide, actorId, isSupervisor, busy, run } = props
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
  // 누른 작업(카드가 고정되어 열린다)과 희망 영역 그리기 상태
  const [selected, setSelected] = useState<{ taskId: string; x: number; y: number } | null>(null)
  const [drawing, setDrawing] = useState<string | null>(null)
  const [drag, setDrag] = useState<{ rowKey: string; x0: number; x1: number } | null>(null)
  // 직접 이동: 끄는 중이거나 놓은 뒤 [확정]을 기다리는 미리보기
  const [move, setMove] = useState<Move | null>(null)
  const dragged = useRef(false)
  // 작업 없애기: [작업 없애기]를 누른 뒤 [확정]을 기다리는 확인 카드. 판정(check)은 서버가 준다
  const [removal, setRemoval] = useState<{
    taskId: string
    x: number
    y: number
    check: RemoveCheck | null
    error: string | null
  } | null>(null)
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
  const placedBy = new Map(plan.assignments.map((a) => [a.task_id, { resourceId: a.resource_id }]))
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
    // 계획 밖 요청의 자리는 서버가 준 기준 시작이다(희망 시작, 희망 영역이 없으면 가장 이른 시작)
    const at = t.base_start ?? t.earliest_start
    bars.push({
      key: `r:${t.task_id}`,
      taskId: t.task_id,
      start: at,
      end: at + t.duration,
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
    const mark = t?.pin ? `${PIN_MARK} ` : ''
    const line1 = `${b.kind === 'after' ? '→ ' : ''}${mark}${b.taskId} ${wt}${b.kind === 'request' ? ' · 요청' : ''}`
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

  // 누르면 선택(같은 작업을 다시 누르면 닫는다). 선택 카드는 그 작업의 기준 막대(후보 변경 후가 아닌 것)로 그린다.
  const select = (taskId: string) => (e: React.MouseEvent) => {
    if (dragged.current) return // 끌어 옮긴 뒤의 click은 선택이 아니다
    setHover(null)
    setDrawing(null)
    setSelected((cur) => (cur?.taskId === taskId ? null : { taskId, x: e.clientX, y: e.clientY }))
  }
  const selectedTask = selected ? taskMap.get(selected.taskId) : undefined
  const selectedBar = selected
    ? (drawn.find((d) => d.b.taskId === selected.taskId && d.b.kind !== 'after') ??
      drawn.find((d) => d.b.taskId === selected.taskId))
    : undefined

  // 희망 영역 그리기: [희망 영역 그리기]를 누른 작업의 행에서만 끈다. 놓으면 분으로 바꿔 서버에 보낸다.
  const startDraw = (rowKey: string, taskId: string) => (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault()
    const rect = e.currentTarget.getBoundingClientRect()
    const at = (clientX: number) => Math.min(Math.max(clientX - rect.left, 0), scale.width)
    const x0 = at(e.clientX)
    setDrag({ rowKey, x0, x1: x0 })
    const move = (ev: PointerEvent) => setDrag({ rowKey, x0, x1: at(ev.clientX) })
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      setDrag(null)
      const x1 = at(ev.clientX)
      const snap = (px: number) => Math.round(scale.minute(px) / DRAW_SNAP_MIN) * DRAW_SNAP_MIN
      const [start, end] = [snap(Math.min(x0, x1)), snap(Math.max(x0, x1))]
      if (end - start < DRAW_SNAP_MIN) return // 너무 짧게 끌면 그리지 않는다
      setDrawing(null)
      void run('희망 영역 그리기', `/tasks/${taskId}/preferred-window`, { start, end })
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  // 직접 이동: 담당자가 자기 작업의 Plan 막대를 끈다. 끌기를 시작할 때 서버에서 놓을 수 있는 구간을 한 번 받고
  // 끄는 동안에는 부르지 않는다. 놓으면 서버가 그 자리를 다시 판정하고, [확정]은 명령이 한 번 더 판정한다.
  const movable = (t: Task | undefined, b: Bar): t is Task =>
    !!t && (b.kind === 'plan' || b.kind === 'before') && t.owner_actor_id === actorId && !t.pin
  const startMove = (t: Task, b: Bar) => (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || busy !== null || drawing !== null) return
    const track = e.currentTarget.closest<HTMLElement>('.tl-track')
    if (!track) return
    const taskId = t.task_id
    const x0 = e.clientX
    const grab = x0 - track.getBoundingClientRect().left - scale.x(b.start)
    let started = false
    let start = b.start
    let options: MoveOptions | null = null
    const at = (clientX: number) => {
      const snap = options?.snap ?? DRAW_SNAP_MIN
      return Math.round(scale.minute(clientX - track.getBoundingClientRect().left - grab) / snap) * snap
    }
    const failed = (err: unknown) =>
      setMove((cur) => (cur?.taskId === taskId ? { ...cur, error: err instanceof Error ? err.message : String(err) } : cur))
    const onMove = (ev: PointerEvent) => {
      if (!started) {
        if (Math.abs(ev.clientX - x0) < DRAG_START_PX) return
        started = true
        dragged.current = true
        setHover(null)
        setSelected(null)
        setMove({
          taskId,
          origin: b.start,
          duration: b.end - b.start,
          start: b.start,
          dropped: false,
          x: ev.clientX,
          y: ev.clientY,
          options: null,
          check: null,
          error: null,
        })
        fetchMoveRange(actorId, taskId)
          .then((o) => {
            options = o
            setMove((cur) => (cur?.taskId === taskId ? { ...cur, options: o } : cur))
          })
          .catch(failed)
      }
      start = at(ev.clientX)
      setMove((cur) => (cur?.taskId === taskId ? { ...cur, start, x: ev.clientX, y: ev.clientY } : cur))
    }
    const onUp = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      if (!started) return
      // 이 pointerup 뒤의 click이 지나간 다음에 푼다
      setTimeout(() => {
        dragged.current = false
      }, 0)
      // 옮길 수 없는 작업이면 사유를 보여 주고, 제자리나 서버가 준 구간 밖에 놓으면 그만둔다
      const refused = options !== null && options.reason_codes.length > 0
      if (!refused && (start === b.start || (options !== null && !inRanges(options, start)))) {
        setMove(null)
        return
      }
      setMove((cur) => (cur?.taskId === taskId ? { ...cur, start, dropped: true, x: ev.clientX, y: ev.clientY } : cur))
      fetchMoveCheck(actorId, taskId, start)
        .then((check) =>
          setMove((cur) => (cur?.taskId === taskId && cur.dropped && cur.start === start ? { ...cur, check } : cur)),
        )
        .catch(failed)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }
  const confirmMove = () => {
    if (!move) return
    const { taskId, start } = move
    setMove(null)
    void run('직접 이동', `/tasks/${taskId}/move`, { start })
  }

  // 작업 없애기: 서버에 없앨 수 있는지 묻고 확인 카드를 띄운다. [확정]하면 계획에 있는 작업은 없애기 명령,
  // 계획 밖 요청은 요청 철회로 보낸다(어느 쪽인지는 서버가 알려 준다).
  const askRemove = (taskId: string) => {
    if (!selected) return
    setRemoval({ taskId, x: selected.x, y: selected.y, check: null, error: null })
    setSelected(null)
    setDrawing(null)
    fetchRemoveCheck(actorId, taskId)
      .then((check) => setRemoval((cur) => (cur?.taskId === taskId ? { ...cur, check } : cur)))
      .catch((err: unknown) =>
        setRemoval((cur) =>
          cur?.taskId === taskId ? { ...cur, error: err instanceof Error ? err.message : String(err) } : cur,
        ),
      )
  }
  const confirmRemove = () => {
    if (!removal?.check) return
    const { taskId, check } = removal
    setRemoval(null)
    if (check.path === 'WITHDRAW') void run(`요청 ${taskId} 철회`, `/tasks/${taskId}/withdraw`, { comment: '' })
    else void run('작업 없애기', `/tasks/${taskId}/remove`, undefined)
  }

  // 권한 안내(UI-04). 버튼은 역할·담당 관계만 보고 켜고, 판정은 서버가 한다(UI-01).
  const actions = (t: Task): ReactNode => {
    const owner = t.owner_actor_id === actorId
    const pinDenied = t.pin
      ? isSupervisor || (owner && t.pin.by_role === 'OWNER')
        ? null
        : t.pin.by_role === 'SUPERVISOR'
          ? 'Supervisor가 건 고정은 Supervisor만 풀 수 있습니다'
          : '담당자 또는 Supervisor만 풀 수 있습니다'
      : isSupervisor || owner
        ? null
        : '담당자 또는 Supervisor만 고정할 수 있습니다'
    const hopeDenied = owner ? null : '희망 영역은 담당자만 그리고 지울 수 있습니다'
    const moveNote = !owner
      ? '담당자만 막대를 끌어 옮길 수 있습니다'
      : t.pin
        ? '고정된 작업은 끌어 옮길 수 없습니다'
        : '막대를 끌어 시각을 옮길 수 있습니다(놓은 뒤 [확정])'
    const removeDenied = !owner
      ? '담당자만 자기 작업을 없앨 수 있습니다'
      : t.pin
        ? '고정된 작업은 먼저 고정을 풀어야 없앨 수 있습니다'
        : null
    const off = busy !== null
    return (
      <>
        <div className="tl-card-actions">
          <button
            className="btn-small"
            disabled={off || pinDenied !== null}
            title={pinDenied ?? undefined}
            onClick={() =>
              void run(t.pin ? '고정 해제' : '작업 고정', `/tasks/${t.task_id}/${t.pin ? 'unpin' : 'pin'}`, undefined)
            }
          >
            {t.pin ? '고정 해제' : '고정'}
          </button>
          {drawing === t.task_id ? (
            <button className="btn-small" onClick={() => setDrawing(null)}>
              그리기 취소
            </button>
          ) : (
            <button
              className="btn-small"
              disabled={off || hopeDenied !== null}
              title={hopeDenied ?? undefined}
              onClick={() => setDrawing(t.task_id)}
            >
              희망 영역 그리기
            </button>
          )}
          <button
            className="btn-small"
            disabled={off || hopeDenied !== null || !t.preferred_window}
            title={hopeDenied ?? (t.preferred_window ? undefined : '희망 영역이 없습니다')}
            onClick={() => void run('희망 영역 지우기', `/tasks/${t.task_id}/preferred-window/clear`, undefined)}
          >
            희망 영역 지우기
          </button>
          <button
            className="btn-small"
            disabled={off || removeDenied !== null}
            title={removeDenied ?? undefined}
            onClick={() => askRemove(t.task_id)}
          >
            작업 없애기
          </button>
        </div>
        {drawing === t.task_id && (
          <p className="small tl-card-note">이 작업의 행에서 끌어 바라는 시각 구간을 그리세요.</p>
        )}
        {pinDenied && <p className="small muted tl-card-note">고정: {pinDenied}</p>}
        {hopeDenied && <p className="small muted tl-card-note">{hopeDenied}</p>}
        <p className="small muted tl-card-note">{moveNote}</p>
        {removeDenied && <p className="small muted tl-card-note">없애기: {removeDenied}</p>}
        <TaskEdit
          key={`${t.task_id}:${t.revision}:${plan.plan_revision}`}
          task={t}
          state={state}
          owner={owner}
          actorId={actorId}
          planned={placedBy.get(t.task_id)}
          busy={busy}
          run={run}
        />
      </>
    )
  }

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
          <span className="lg lg-pinned" title="사람이 고정한 작업. 재계획이 움직이지 않는다">
            {PIN_MARK} 고정
          </span>
          <span
            className="lg lg-hope"
            title="희망 영역. 강제하지 않지만 벗어난 만큼이 지연으로 계산되고, 말한 희망의 범위 안이면 묻지 않는다. 점선 테두리는 Agent가 정했고 아직 확인하지 않은 희망"
          >
            희망 영역
          </span>
          <span className="lg lg-window" title="가능 범위(반드시 지켜야 하는 시간창). 사람이 좁힌 작업은 늘, 나머지는 눌렀을 때 보인다">
            가능 범위
          </span>
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
            const rowKey = `${row.group}:${row.id}`
            // 이 행에 있는 작업의 기준 막대(후보 변경 후가 아닌 것): 그리기·직접 이동의 자리
            const baseBars = rowBars.filter((d) => d.b.kind !== 'after' && d.t)
            // 희망 영역·가능 범위는 후보가 옮긴 자리(변경 후 막대)에도 같이 그린다
            const rangeBars = rowBars.filter((d) => d.t)
            const horizon = state.site.horizon_minutes
            const laneTop = (key: string) => strip + 2 + (laneOf.get(key) ?? 0) * LANE_PX
            const drawable = drawing !== null && baseBars.some((d) => d.b.taskId === drawing)
            return (
              <div key={rowKey} className={`tl-row ${sep ? 'tl-sep' : ''}`}>
                <div className="tl-label" style={{ width: LABEL_W }}>
                  {row.label}
                </div>
                <div
                  className={`tl-track ${drawable ? 'tl-draw' : ''}`}
                  style={{ width: scale.width, height: strip + laneCount * LANE_PX }}
                  onPointerDown={drawable && drawing ? startDraw(rowKey, drawing) : undefined}
                >
                  {background}
                  {rangeBars.map((d) => {
                    const w = d.t?.preferred_window
                    const box = w ? scale.box(w.start, w.end) : null
                    if (!w || !box) return null
                    const decided = w.origin === 'DECIDED'
                    return (
                      <div
                        key={`hope:${d.b.key}`}
                        className={`tl-hope ${decided ? 'tl-hope-decided' : ''} unit-${unitIndex.get(d.t?.unit_id ?? '') ?? UNIT_COLORS - 1}`}
                        style={{ left: box.left, width: box.width, top: laneTop(d.b.key) - 1, height: BAR_H + 2 }}
                        title={`${d.b.taskId} 희망 영역 ${clock.span(w.start, w.end)}${decided ? ' · Agent가 정함(확인 전)' : ''}`}
                      />
                    )
                  })}
                  {rangeBars.map((d) => {
                    if (!d.t) return null
                    // 사람이 좁힌 가능 범위는 늘, Horizon 전체는 눌렀을 때만. 후보가 옮긴 자리에는 늘 그린다
                    const narrowed = d.t.earliest_start > 0 || d.t.latest_end < horizon
                    if (!narrowed && selected?.taskId !== d.b.taskId) return null
                    const box = scale.box(d.t.earliest_start, d.t.latest_end)
                    if (!box) return null
                    return (
                      <div
                        key={`win:${d.b.key}`}
                        className={`tl-window ${box.clipL ? 'tl-window-clip-l' : ''} ${box.clipR ? 'tl-window-clip-r' : ''}`}
                        style={{ left: box.left, width: box.width, top: laneTop(d.b.key) + BAR_H - 3 }}
                        title={`${d.b.taskId} 가능 범위 ${clock.span(d.t.earliest_start, d.t.latest_end)}`}
                      />
                    )
                  })}
                  {move &&
                    baseBars
                      .filter((d) => d.b.taskId === move.taskId && d.b.kind !== 'request')
                      .map((d) => {
                        const top = laneTop(d.b.key)
                        const ghost = scale.box(move.start, move.start + move.duration)
                        const bad = move.check
                          ? !move.check.ok
                          : move.options !== null && !inRanges(move.options, move.start)
                        return (
                          <div key={`move:${d.b.key}`}>
                            {move.options?.ranges.map((r) => {
                              const box = scale.box(r.start_min, r.start_max + move.duration)
                              return box ? (
                                <div
                                  key={r.start_min}
                                  className="tl-drop"
                                  style={{ left: box.left, width: box.width, top: top - 1, height: BAR_H + 2 }}
                                />
                              ) : null
                            })}
                            {ghost && (
                              <div
                                className={`tl-ghost ${bad ? 'tl-ghost-bad' : ''}`}
                                style={{ left: ghost.left, width: ghost.width, top, height: BAR_H }}
                              >
                                {clock.hm(move.start)}–{clock.hm(move.start + move.duration)}
                              </div>
                            )}
                          </div>
                        )
                      })}
                  {drag && drag.rowKey === rowKey && (
                    <div
                      className="tl-drag"
                      style={{ left: Math.min(drag.x0, drag.x1), width: Math.abs(drag.x1 - drag.x0) }}
                    />
                  )}
                  {rowConflicts.map((c) => {
                    const box = scale.box(c.interval[0], c.interval[1])
                    if (!box) return null
                    return (
                      <div
                        key={`${c.rule_id}:${c.task_ids.join(',')}:${c.pool?.pool_id ?? ''}`}
                        className="tl-conflict"
                        style={{ left: box.left, width: box.width }}
                        title={`${ruleLabel(c.rule_id)} (${c.rule_id}) · ${c.task_ids.join(', ')} · ${clock.span(c.interval[0], c.interval[1])}${c.pool ? ` · ${poolExcessText(meta, c)} (${clock.format(c.pool.at)}부터)` : ''}`}
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
                            t?.pin ? 'bar-pinned' : '',
                            movable(t, b) ? 'bar-movable' : '',
                            move?.taskId === b.taskId && b.kind !== 'after' ? 'bar-moving' : '',
                            selected?.taskId === b.taskId ? 'bar-selected' : '',
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
                          onClick={select(b.taskId)}
                          onPointerDown={movable(t, b) ? startMove(t, b) : undefined}
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
                            onClick={select(b.taskId)}
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
      {hovered && hover && hovered.b.taskId !== selected?.taskId && drawing === null && move === null && (
        <BarCard
          d={hovered}
          x={hover.x}
          y={hover.y}
          owner={actorName.get(hovered.t?.owner_actor_id ?? '') ?? hovered.t?.owner_actor_id ?? '—'}
          unit={unitName.get(hovered.t?.unit_id ?? '') ?? hovered.t?.unit_id ?? '—'}
          conflicts={conflicts.filter((c) => c.task_ids.includes(hovered.b.taskId))}
          ruleLabel={ruleLabel}
          actorName={actorName}
          horizon={state.site.horizon_minutes}
        />
      )}
      {selected && selectedTask && selectedBar && (
        <BarCard
          d={selectedBar}
          x={selected.x}
          y={selected.y}
          owner={actorName.get(selectedTask.owner_actor_id) ?? selectedTask.owner_actor_id}
          unit={unitName.get(selectedTask.unit_id) ?? selectedTask.unit_id}
          conflicts={conflicts.filter((c) => c.task_ids.includes(selected.taskId))}
          ruleLabel={ruleLabel}
          actorName={actorName}
          horizon={state.site.horizon_minutes}
          onClose={() => {
            setSelected(null)
            setDrawing(null)
          }}
        >
          {actions(selectedTask)}
        </BarCard>
      )}
      {removal && (
        <div
          className="tl-card tl-card-fixed"
          style={{
            width: 340,
            left: removal.x + 14 + 340 > window.innerWidth ? removal.x - 14 - 340 : removal.x + 14,
            top: Math.max(8, Math.min(removal.y + 14, window.innerHeight - 220)),
          }}
        >
          <div className="tl-card-head">
            <span className="strong">{removal.taskId} 작업 없애기</span>
          </div>
          {!removal.check && !removal.error && <p className="small muted tl-card-note">서버가 확인하는 중…</p>}
          {removal.error && <p className="small tl-card-note">확인하지 못했습니다: {removal.error}</p>}
          {removal.check && !removal.check.ok && (
            <p className="small tl-card-note">
              없앨 수 없습니다: {removal.check.reason_codes.map((c) => ruleLabel(c)).join(', ')}
            </p>
          )}
          {removal.check?.ok && (
            <>
              <p className="small tl-card-note">
                {removal.check.path === 'WITHDRAW'
                  ? '계획에 없는 요청입니다. 요청을 철회해 계산 대상에서 뺍니다.'
                  : '이 작업을 계획에서 뺍니다. 자기 작업만 바뀌므로 Supervisor 승인 없이 확정됩니다.'}
              </p>
              {removal.check.invalidates.length > 0 && (
                <p className="small tl-card-note">
                  확정하면 검토 중인 안 {removal.check.invalidates.length}개가 무효가 됩니다:{' '}
                  {removal.check.invalidates.map((id) => id.slice(0, 13)).join(', ')}
                </p>
              )}
              <p className="small muted tl-card-note">되돌릴 수 없습니다.</p>
            </>
          )}
          <div className="tl-card-actions">
            <button className="btn-small" disabled={!removal.check?.ok || busy !== null} onClick={confirmRemove}>
              확정
            </button>
            <button className="btn-small" onClick={() => setRemoval(null)}>
              취소
            </button>
          </div>
        </div>
      )}
      {move?.dropped && (
        <div
          className="tl-card tl-card-fixed"
          style={{
            width: 340,
            left: move.x + 14 + 340 > window.innerWidth ? move.x - 14 - 340 : move.x + 14,
            top: Math.max(8, Math.min(move.y + 14, window.innerHeight - 220)),
          }}
        >
          <div className="tl-card-head">
            <span className="strong">{move.taskId} 직접 이동</span>
          </div>
          <p className="small tl-card-note">
            시작 {clock.format(move.origin)} → {clock.format(move.start)}
          </p>
          {!move.check && !move.error && <p className="small muted tl-card-note">서버가 이 자리를 확인하는 중…</p>}
          {move.error && <p className="small tl-card-note">확인하지 못했습니다: {move.error}</p>}
          {move.check && !move.check.ok && (
            <p className="small tl-card-note">
              옮길 수 없습니다: {move.check.reason_codes.map((c) => ruleLabel(c)).join(', ')}
            </p>
          )}
          {move.check?.ok && (
            <>
              {move.check.invalidates.length > 0 && (
                <p className="small tl-card-note">
                  확정하면 검토 중인 안 {move.check.invalidates.length}개가 무효가 됩니다:{' '}
                  {move.check.invalidates.map((id) => id.slice(0, 13)).join(', ')}
                </p>
              )}
              <p className="small muted tl-card-note">
                자기 작업의 시각만 바뀌므로 Supervisor 승인 없이 확정됩니다. 작업은 고정되지 않습니다.
              </p>
            </>
          )}
          <div className="tl-card-actions">
            <button className="btn-small" disabled={!move.check?.ok || busy !== null} onClick={confirmMove}>
              확정
            </button>
            <button className="btn-small" onClick={() => setMove(null)}>
              취소
            </button>
          </div>
        </div>
      )}
    </section>
  )
}

/** 작업 카드: 작업·담당·시각·자원·고정·희망 영역·Gate·충돌. 화면 밖으로 나가지 않게 뒤집는다.
 *  마우스를 올리면 잠깐 나오고, 막대를 누르면(onClose가 있으면) 고정되어 열려 버튼(children)을 쓸 수 있다. */
function BarCard({
  d,
  x,
  y,
  owner,
  unit,
  conflicts,
  ruleLabel,
  actorName,
  horizon,
  onClose,
  children,
}: {
  d: { b: Bar; t: Task | undefined; wt: string }
  x: number
  y: number
  owner: string
  unit: string
  conflicts: Conflict[]
  ruleLabel: (id: string) => string
  actorName: Map<string, string>
  horizon: number
  onClose?: () => void
  children?: ReactNode
}) {
  const { clock, meta } = useEnv()
  const { b, t } = d
  const CARD_W = 340
  const left = x + 14 + CARD_W > window.innerWidth ? x - 14 - CARD_W : x + 14
  const top = Math.max(8, Math.min(y + 14, window.innerHeight - (onClose ? 520 : 280)))
  // 고정한 시각(서버 시각, UTC)은 현장 시각으로 바꿔 보여 준다
  const pinnedAt = t?.pin ? clock.format(Math.round((Date.parse(t.pin.pinned_at) - clock.originMs) / 60_000)) : ''
  return (
    <div className={`tl-card ${onClose ? 'tl-card-fixed' : ''}`} style={{ left, top, width: CARD_W }}>
      <div className="tl-card-head">
        <b>
          {b.taskId} {d.wt}
        </b>
        <span className="muted small">
          {KIND_LABEL[b.kind]}
          {onClose && (
            <button className="tl-card-close" onClick={onClose} title="닫기">
              ×
            </button>
          )}
        </span>
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
            <th>가능 범위</th>
            <td>
              {!t
                ? '—'
                : t.earliest_start <= 0 && t.latest_end >= horizon
                  ? '전체 기간(좁히지 않음)'
                  : clock.span(t.earliest_start, t.latest_end)}
            </td>
          </tr>
          <tr>
            <th>고정</th>
            <td>
              {t?.pin
                ? `${PIN_MARK} ${actorName.get(t.pin.pinned_by) ?? t.pin.pinned_by}${t.pin.by_role === 'SUPERVISOR' ? ' (Supervisor)' : ''} · ${pinnedAt}`
                : '고정되지 않음(재계획이 움직일 수 있다)'}
            </td>
          </tr>
          <tr>
            <th>희망 영역</th>
            <td>
              {t?.preferred_window ? clock.span(t.preferred_window.start, t.preferred_window.end) : '없음'}
              {t?.preferred_window?.origin === 'DECIDED' && <span className="tag tag-warn"> 정함(확인 전)</span>}
              {t?.preferred_window?.origin === 'STATED' && t.preferred_window.made_by === 'INTAKE' && (
                <span className="muted"> · 요청 문장에서</span>
              )}
            </td>
          </tr>
          {t && decidedValues(t).length > 0 && (
            <tr>
              <th>Agent가 정한 값</th>
              <td>
                {decidedValues(t)
                  .map((name) => VALUE_NAME[name] ?? name)
                  .join(', ')}
              </td>
            </tr>
          )}
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
                    <div key={`${c.rule_id}:${c.task_ids.join(',')}:${c.pool?.pool_id ?? ''}`}>
                      {ruleLabel(c.rule_id)} · {c.task_ids.join(', ')} · {clock.span(c.interval[0], c.interval[1])}
                      {c.pool && ` · ${poolExcessText(meta, c)} (${clock.format(c.pool.at)}부터)`}
                    </div>
                  ))}
            </td>
          </tr>
        </tbody>
      </table>
      {children}
    </div>
  )
}
