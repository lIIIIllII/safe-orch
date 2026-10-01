// Horizon 원점 기준 분 ↔ 현장 시각 변환 (§5.1, 부록 A.19·A.20 2차).
// 시간대·원점·근무 구간은 모두 meta(GET /api/sites/{id}/meta)에서 받는다. Pack 값을 상수로 두지 않는다.

import type { Meta } from './types'

const WEEKDAY_FALLBACK = ['일', '월', '화', '수', '목', '금', '토']

interface Local {
  y: number
  mo: number
  d: number
  h: number
  mi: number
  wd: string
}

export interface WorkDay {
  index: number
  key: string // YYYY-MM-DD (현장 시간대)
  label: string // 10/13(화)
  lo: number
  hi: number
}

/** 현장 시계. meta가 바뀌지 않으므로 앱 시작 때 한 번 만든다. */
export class Clock {
  readonly tz: string
  readonly originMs: number
  readonly workIntervals: [number, number][]
  readonly workDays: WorkDay[]
  private readonly fmt: Intl.DateTimeFormat

  constructor(meta: Meta) {
    this.tz = meta.timezone
    this.originMs = Date.parse(meta.horizon_start_utc)
    this.workIntervals = meta.work_intervals
    this.fmt = new Intl.DateTimeFormat('ko-KR', {
      timeZone: this.tz,
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      weekday: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    })
    this.workDays = this.workIntervals.map(([lo, hi], index) => ({
      index,
      key: this.dateKey(lo),
      label: this.dayLabel(lo),
      lo,
      hi,
    }))
  }

  private partsAt(ms: number): Local {
    const p: Record<string, string> = {}
    for (const part of this.fmt.formatToParts(new Date(ms))) p[part.type] = part.value
    const y = Number(p.year)
    const mo = Number(p.month)
    const d = Number(p.day)
    const wd = p.weekday ?? WEEKDAY_FALLBACK[new Date(Date.UTC(y, mo - 1, d)).getUTCDay()]
    return { y, mo, d, h: Number(p.hour) % 24, mi: Number(p.minute), wd }
  }

  local(minute: number): Local {
    return this.partsAt(this.originMs + minute * 60_000)
  }

  hm(minute: number): string {
    const t = this.local(minute)
    return `${String(t.h).padStart(2, '0')}:${String(t.mi).padStart(2, '0')}`
  }

  dayLabel(minute: number): string {
    const t = this.local(minute)
    return `${t.mo}/${t.d}(${t.wd})`
  }

  dateKey(minute: number): string {
    const t = this.local(minute)
    return `${t.y}-${String(t.mo).padStart(2, '0')}-${String(t.d).padStart(2, '0')}`
  }

  /** "10/13(화) 14:00" */
  format(minute: number): string {
    return `${this.dayLabel(minute)} ${this.hm(minute)}`
  }

  /** 같은 날이면 "10/13(화) 14:00–15:00", 날짜를 넘으면 "10/13(화) 14:00 → 10/14(수) 09:00". */
  span(start: number, end: number): string {
    if (this.dateKey(start) === this.dateKey(end) || this.isMidnight(end, start)) {
      return `${this.format(start)}–${this.hm(end)}`
    }
    return `${this.format(start)} → ${this.format(end)}`
  }

  private isMidnight(end: number, start: number): boolean {
    // 자정에 끝나면 같은 날로 본다(종료는 [start, end)의 바깥 경계)
    return this.hm(end) === '00:00' && this.dateKey(end - 1) === this.dateKey(start)
  }

  /** 현장 날짜(YYYY-MM-DD) + "HH:MM" → 원점 기준 분. 형식이 틀리면 null. */
  toMinute(dateKey: string, clock: string): number | null {
    const dm = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateKey)
    const cm = /^(\d{1,2}):(\d{2})$/.exec(clock.trim())
    if (!dm || !cm) return null
    const [h, mi] = [Number(cm[1]), Number(cm[2])]
    if (h > 23 || mi > 59) return null
    const guess = Date.UTC(Number(dm[1]), Number(dm[2]) - 1, Number(dm[3]), h, mi)
    const offset = (ms: number) => {
      const t = this.partsAt(ms)
      return Date.UTC(t.y, t.mo - 1, t.d, t.h, t.mi) - ms
    }
    // 시간대 오프셋은 그 시각 기준이다. 두 번 맞춰 일광절약 경계도 처리한다.
    const utc = guess - offset(guess - offset(guess))
    return Math.round((utc - this.originMs) / 60_000)
  }

  /** 근무 구간 하나에 [start, end)가 들어가는가 (서버 CALENDAR와 같은 정의, 안내 표시용). */
  inWork(start: number, end: number): boolean {
    return this.workIntervals.some(([lo, hi]) => lo <= start && end <= hi)
  }

  /** 분이 속한 근무일 index: 그날 근무 시작 1시간 전부터 다음 근무일의 같은 지점 전까지. */
  dayIndex(minute: number): number {
    let i = 0
    this.workDays.forEach((d) => {
      if (d.lo - 60 <= minute) i = d.index
    })
    return i
  }
}

/** 서버가 내려준 달력 분 지연과 근무 분 지연. 같으면 하나만 (A.20, 화면에서 계산하지 않는다). */
export function delayText(delay: number | null | undefined, workDelay: number | null | undefined): string {
  if (delay === null || delay === undefined) return '—'
  if (workDelay === null || workDelay === undefined || workDelay === delay) return `${delay}분`
  return `${delay}분 (근무시간 기준 ${workDelay}분)`
}
