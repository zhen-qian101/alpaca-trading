import { describe, expect, it } from 'vitest'

import { TICK, tickLabel } from '../features/terminal/chart/chartMath.ts'
import {
  addDays,
  etToday,
  formatAge,
  formatCountdown,
  formatDateTime,
  formatLongDateTime,
  zonedWallTimeToUtcMs,
} from './time.ts'

// 2026-03-04T20:00:00Z: 15:00 in New York (EST, UTC-5), 07:00 next day in Melbourne (AEDT, UTC+11).
const BAR = Date.UTC(2026, 2, 4, 20) / 1000

describe('zone formatting of TRUE UTC epochs', () => {
  it('formats the same instant in ET and in Melbourne', () => {
    expect(formatDateTime(BAR, 'et')).toBe('4 Mar 15:00')
    expect(formatDateTime(BAR, 'melbourne')).toBe('5 Mar 07:00')
    expect(formatLongDateTime(BAR, 'et')).toBe('Wed 4 Mar 2026 15:00')
  })

  it('follows each zone across its own DST change', () => {
    // US clocks went forward on 2026-03-08; Melbourne's go back on 2026-04-05.
    const afterUsDst = Date.UTC(2026, 2, 9, 14) / 1000
    expect(formatDateTime(afterUsDst, 'et')).toBe('9 Mar 10:00')
    expect(formatDateTime(afterUsDst, 'melbourne')).toBe('10 Mar 01:00')
    const afterAuDst = Date.UTC(2026, 3, 6, 14) / 1000
    expect(formatDateTime(afterAuDst, 'melbourne')).toBe('7 Apr 00:00')
  })

  it('labels chart ticks in the chosen zone, not UTC', () => {
    expect(tickLabel(BAR, TICK.Time, 'et')).toBe('15:00')
    expect(tickLabel(BAR, TICK.Time, 'melbourne')).toBe('07:00')
    expect(tickLabel(BAR, TICK.DayOfMonth, 'melbourne')).toBe('5 Mar')
    expect(tickLabel(BAR, TICK.Month, 'et')).toBe('Mar')
  })

  it('renders missing times as a dash', () => {
    expect(formatDateTime(null, 'et')).toBe('—')
  })
})

describe('ET wall clock', () => {
  it('finds the UTC instant of 20:00 ET in winter and in summer', () => {
    expect(new Date(zonedWallTimeToUtcMs('2026-03-04', 20, 0, 'America/New_York')).toISOString()).toBe(
      '2026-03-05T01:00:00.000Z',
    )
    expect(new Date(zonedWallTimeToUtcMs('2026-07-01', 20, 0, 'America/New_York')).toISOString()).toBe(
      '2026-07-02T00:00:00.000Z',
    )
  })

  it("knows today's session date in ET", () => {
    expect(etToday(Date.parse('2026-03-05T03:00:00Z'))).toBe('2026-03-04')
    expect(addDays('2026-02-28', 1)).toBe('2026-03-01')
  })
})

describe('durations', () => {
  it('formats ages compactly', () => {
    expect(formatAge(42_000)).toBe('42 s')
    expect(formatAge(12 * 60_000)).toBe('12 min')
    expect(formatAge(5 * 3_600_000)).toBe('5 h')
    expect(formatAge(3 * 86_400_000)).toBe('3 d')
  })

  it('formats countdowns', () => {
    expect(formatCountdown(3 * 3_600_000 + 12 * 60_000 + 5_000)).toBe('3 h 12 min')
    expect(formatCountdown(45_000)).toBe('45 s')
  })
})
