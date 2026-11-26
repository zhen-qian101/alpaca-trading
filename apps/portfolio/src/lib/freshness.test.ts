import { describe, expect, it } from 'vitest'

import { isStale, NORMAL_REFRESH_HOURS, STALE_AFTER_MS } from './freshness.ts'

const HOUR = 3_600_000
const asOf = '2026-06-12T22:05:00Z' // a Friday evening, New York time
const at = (hours: number) => Date.parse(asOf) + hours * HOUR

describe('isStale', () => {
  it('allows the longest healthy gap and a few delayed or dropped runs', () => {
    // 6 h between scheduled deploys + 1 h heartbeat + 15 min export
    expect(isStale(asOf, at(NORMAL_REFRESH_HOURS + 1.25))).toBe(false)
    // three scheduled runs in a row lost
    expect(isStale(asOf, at(4 * NORMAL_REFRESH_HOURS + 1.25))).toBe(false)
    expect(isStale(asOf, at(25.9))).toBe(false)
  })

  it('flags figures older than the threshold, weekend or not', () => {
    expect(STALE_AFTER_MS).toBe(26 * HOUR)
    expect(isStale(asOf, at(26.1))).toBe(true)
    // Monday morning after a Friday-evening export: the pipeline publishes
    // through the weekend, so this is a fault, not a closed market.
    expect(isStale(asOf, Date.parse('2026-06-15T13:00:00Z'))).toBe(true)
  })

  it('never flags an unreadable timestamp or a clock behind the file', () => {
    expect(isStale('not a time', at(100))).toBe(false)
    expect(isStale(asOf, at(-5))).toBe(false)
  })
})
