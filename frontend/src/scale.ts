// 타임라인 위치 계산. 분 → x(px). 화면 폭에 맞추지 않고 분당 픽셀로
// 그리며 넘치면 가로 스크롤한다. "근무시간 맞춤"만 패널 폭에서 분당 픽셀을 거꾸로 계산한다.
// 하루 보기: 그날 근무 구간 앞뒤 VIEW_MARGIN_MIN을 함께 보여 준다(앞뒤는 비근무 표시). 맞춤이면 근무 구간만.
// 전체 보기: 근무 구간을 잇고 근무일 사이 밤은 NIGHT_STRIP_PX 폭 띠로 접는다.

import type { Clock, WorkDay } from './time'

export const VIEW_MARGIN_MIN = 60
export const NIGHT_STRIP_PX = 24
const MIN_BAR_PX = 3
const EDGE_MARKER_PX = 14
/** 하루 보기 분당 픽셀 단계와 기본값(30분 = 90px), 전체 보기 기본값 */
export const DAY_ZOOMS = [2, 3, 4, 6] as const
export const DAY_ZOOM_DEFAULT = 3
export const ALL_ZOOM_DEFAULT = 1

export type View = { mode: 'day'; day: number } | { mode: 'all' }

interface Seg {
  lo: number
  hi: number
  off: boolean // 비근무
  fixedPx: number | null // 접힌 밤 띠(고정 폭). null이면 시간에 비례
}

export interface Box {
  left: number
  width: number
  clipL: boolean
  clipR: boolean
}

export interface Tick {
  m: number
  label: string | null
}

export class Scale {
  readonly start: number
  readonly end: number
  readonly segs: Seg[]
  readonly pxPerMin: number
  /** 그리는 전체 폭(px) */
  readonly width: number

  constructor(segs: Seg[], pxPerMin: number) {
    this.segs = segs
    this.pxPerMin = pxPerMin
    this.start = segs[0].lo
    this.end = segs[segs.length - 1].hi
    this.width = segs.reduce((n, s) => n + (s.fixedPx ?? (s.hi - s.lo) * pxPerMin), 0)
  }

  /** 분 → x(px). 범위 밖은 가장자리로 붙인다. 접힌 밤은 고정 폭 안에서 비례. */
  x(minute: number): number {
    const m = Math.min(Math.max(minute, this.start), this.end)
    let px = 0
    for (const s of this.segs) {
      const part = Math.min(Math.max(m - s.lo, 0), s.hi - s.lo)
      px += s.fixedPx === null ? part * this.pxPerMin : (part / (s.hi - s.lo)) * s.fixedPx
      if (m < s.hi) break
    }
    return px
  }

  /** x(px) → 분. x()의 역이다(막대 끌어 옮기기). 범위 밖은 가장자리로 붙인다. */
  minute(px: number): number {
    let left = Math.min(Math.max(px, 0), this.width)
    for (const s of this.segs) {
      const w = s.fixedPx ?? (s.hi - s.lo) * this.pxPerMin
      if (left <= w) return s.lo + (w === 0 ? 0 : (left / w) * (s.hi - s.lo))
      left -= w
    }
    return this.end
  }

  /** [start, end)의 left·width(px). 범위와 겹치지 않으면 null. 아주 짧으면 최소 폭. */
  box(start: number, end: number): Box | null {
    if (end <= this.start || start >= this.end) return null
    const left = this.x(start)
    return {
      left,
      width: Math.max(MIN_BAR_PX, this.x(end) - left),
      clipL: start < this.start,
      clipR: end > this.end,
    }
  }

  /** 범위 밖 작업을 가장자리 표시로 (하루 보기). */
  edge(side: 'left' | 'right'): { left: number; width: number } {
    return { left: side === 'left' ? 0 : this.width - EDGE_MARKER_PX, width: EDGE_MARKER_PX }
  }
}

/** 하루 보기. fitPx가 있으면 "근무시간 맞춤": 근무 구간만 그 폭에 맞춘다. */
export function dayScale(day: WorkDay, pxPerMin: number, fitPx: number | null = null): Scale {
  if (fitPx !== null) {
    return new Scale([{ lo: day.lo, hi: day.hi, off: false, fixedPx: null }], fitPx / (day.hi - day.lo))
  }
  return new Scale(
    [
      { lo: day.lo - VIEW_MARGIN_MIN, hi: day.lo, off: true, fixedPx: null },
      { lo: day.lo, hi: day.hi, off: false, fixedPx: null },
      { lo: day.hi, hi: day.hi + VIEW_MARGIN_MIN, off: true, fixedPx: null },
    ],
    pxPerMin,
  )
}

/** 전체 보기. fitPx가 있으면 근무 구간 합을 (폭 − 밤 띠)에 맞춘다. */
export function allScale(days: WorkDay[], pxPerMin: number, fitPx: number | null = null): Scale {
  const segs: Seg[] = []
  days.forEach((d, i) => {
    if (i > 0) segs.push({ lo: days[i - 1].hi, hi: d.lo, off: true, fixedPx: NIGHT_STRIP_PX })
    segs.push({ lo: d.lo, hi: d.hi, off: false, fixedPx: null })
  })
  if (fitPx === null) return new Scale(segs, pxPerMin)
  const work = days.reduce((n, d) => n + (d.hi - d.lo), 0)
  const nights = NIGHT_STRIP_PX * Math.max(0, days.length - 1)
  return new Scale(segs, Math.max(0.1, (fitPx - nights) / work))
}

/** 눈금: 하루 보기 15분 선·30분 라벨, 전체 보기 1시간 선·라벨(1시간 폭이 좁으면 3시간 라벨, 근무 구간 안). */
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
      const every = scale.pxPerMin * 60 >= 48 ? 60 : 180
      out.push({ m, label: (m - s.lo) % every === 0 && m < s.hi ? clock.hm(m) : null })
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
): (Box & { edge: 'left' | 'right' | null }) | null {
  const box = scale.box(start, end)
  if (box) return { ...box, edge: null }
  if (view.mode !== 'day') return null
  if (clock.dayIndex(start) !== view.day) return null
  const side = end <= scale.start ? 'left' : 'right'
  return { ...scale.edge(side), clipL: side === 'left', clipR: side === 'right', edge: side }
}
