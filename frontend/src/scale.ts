// 타임라인 위치 계산 (부록 A.20 2차). "1분 = grid 1칸"을 버리고 분 → x(퍼센트 + 고정 px)로 그린다.
// 하루 보기: 그날 근무 구간 앞뒤 VIEW_MARGIN_MIN을 함께 보여 준다(앞뒤는 비근무 표시).
// 전체 보기: 근무 구간을 잇고 근무일 사이 밤은 NIGHT_STRIP_PX 폭 띠로 접는다.

import type { Clock, WorkDay } from './time'

export const VIEW_MARGIN_MIN = 60
export const NIGHT_STRIP_PX = 24
const MIN_BAR_PX = 3
const EDGE_MARKER_PX = 14

export type View = { mode: 'day'; day: number } | { mode: 'all' }

interface Seg {
  lo: number
  hi: number
  off: boolean // 비근무
  fixedPx: number | null // 접힌 밤 띠(고정 폭). null이면 시간에 비례
}

export interface X {
  pct: number
  px: number
}

export const css = (x: X) => `calc(${x.pct.toFixed(4)}% + ${x.px.toFixed(2)}px)`
const sub = (a: X, b: X): X => ({ pct: a.pct - b.pct, px: a.px - b.px })

export interface Tick {
  m: number
  label: string | null
}

export class Scale {
  readonly start: number
  readonly end: number
  readonly segs: Seg[]
  private readonly propTotal: number
  private readonly fixedTotal: number

  constructor(segs: Seg[]) {
    this.segs = segs
    this.start = segs[0].lo
    this.end = segs[segs.length - 1].hi
    this.propTotal = segs.filter((s) => s.fixedPx === null).reduce((n, s) => n + (s.hi - s.lo), 0)
    this.fixedTotal = segs.reduce((n, s) => n + (s.fixedPx ?? 0), 0)
  }

  /** 분 → x. 범위 밖은 가장자리로 붙인다. 비례 구간 폭 = (100% − 고정 px 합) × 분 / 비례 분 합. */
  x(minute: number): X {
    const m = Math.min(Math.max(minute, this.start), this.end)
    let w = 0
    let px = 0
    for (const s of this.segs) {
      const part = Math.min(Math.max(m - s.lo, 0), s.hi - s.lo)
      if (s.fixedPx === null) w += part
      else px += (part / (s.hi - s.lo)) * s.fixedPx
      if (m < s.hi) break
    }
    const frac = this.propTotal > 0 ? w / this.propTotal : 0
    return { pct: frac * 100, px: px - frac * this.fixedTotal }
  }

  /** [start, end)의 left·width. 범위와 겹치지 않으면 null. 아주 짧으면 최소 폭. */
  box(start: number, end: number): { left: string; width: string; clipL: boolean; clipR: boolean } | null {
    if (end <= this.start || start >= this.end) return null
    const a = this.x(start)
    const w = sub(this.x(end), a)
    return {
      left: css(a),
      width: `max(${MIN_BAR_PX}px, ${css(w)})`,
      clipL: start < this.start,
      clipR: end > this.end,
    }
  }

  /** 범위 밖 작업을 가장자리 표시로 (하루 보기). */
  edge(side: 'left' | 'right'): { left: string; width: string } {
    const x = side === 'left' ? this.x(this.start) : { ...this.x(this.end), px: this.x(this.end).px - EDGE_MARKER_PX }
    return { left: css(x), width: `${EDGE_MARKER_PX}px` }
  }
}

export function dayScale(day: WorkDay): Scale {
  return new Scale([
    { lo: day.lo - VIEW_MARGIN_MIN, hi: day.lo, off: true, fixedPx: null },
    { lo: day.lo, hi: day.hi, off: false, fixedPx: null },
    { lo: day.hi, hi: day.hi + VIEW_MARGIN_MIN, off: true, fixedPx: null },
  ])
}

export function allScale(days: WorkDay[]): Scale {
  const segs: Seg[] = []
  days.forEach((d, i) => {
    if (i > 0) segs.push({ lo: days[i - 1].hi, hi: d.lo, off: true, fixedPx: NIGHT_STRIP_PX })
    segs.push({ lo: d.lo, hi: d.hi, off: false, fixedPx: null })
  })
  return new Scale(segs)
}

/** 눈금: 하루 보기 15분 선·30분 라벨, 전체 보기 1시간 선·3시간 라벨(근무 구간 안). */
export function ticks(scale: Scale, view: View, clock: Clock): Tick[] {
  const out: Tick[] = []
  if (view.mode === 'day') {
    for (let m = scale.start; m <= scale.end; m += 15) {
      out.push({ m, label: (m - scale.start) % 30 === 0 ? clock.hm(m) : null })
    }
    return out
  }
  for (const s of scale.segs) {
    if (s.off) continue
    for (let m = s.lo; m <= s.hi; m += 60) {
      out.push({ m, label: (m - s.lo) % 180 === 0 && m < s.hi ? clock.hm(m) : null })
    }
  }
  return out
}

/** 작업을 어느 보기 위치에 그릴지: 겹치면 상자, 하루 보기에서 그날 몫인데 범위 밖이면 가장자리 표시. */
export function placement(
  scale: Scale,
  view: View,
  clock: Clock,
  start: number,
  end: number,
): { left: string; width: string; clipL: boolean; clipR: boolean; edge: 'left' | 'right' | null } | null {
  const box = scale.box(start, end)
  if (box) return { ...box, edge: null }
  if (view.mode !== 'day') return null
  if (clock.dayIndex(start) !== view.day) return null
  const side = end <= scale.start ? 'left' : 'right'
  return { ...scale.edge(side), clipL: side === 'left', clipR: side === 'right', edge: side }
}
