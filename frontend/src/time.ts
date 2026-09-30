// Horizon 원점 기준 분 ↔ 현장 시각(Asia/Seoul) 변환 (§5.1, 부록 A.5·A.19).

const formatter = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

export function minuteToClock(horizonStartUtc: string, minute: number): string {
  const t = new Date(Date.parse(horizonStartUtc) + minute * 60_000)
  return formatter.format(t)
}

/** "HH:MM" → 원점 기준 분. 형식이 틀리면 null. */
export function clockToMinute(horizonStartUtc: string, clock: string): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(clock.trim())
  if (!m) return null
  const origin = minuteToClock(horizonStartUtc, 0)
  const [oh, om] = origin.split(':').map(Number)
  return Number(m[1]) * 60 + Number(m[2]) - (oh * 60 + om)
}

export function span(horizonStartUtc: string, start: number, end: number): string {
  return `${minuteToClock(horizonStartUtc, start)}–${minuteToClock(horizonStartUtc, end)}`
}
