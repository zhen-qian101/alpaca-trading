import { describe, expect, it } from 'vitest'

import { FRESHNESS } from '../contract.ts'
import { ageMs, freshnessOf, fuseRemaining, pageFreshness, RESUME_CHECK_MS } from './freshness.ts'

const AS_OF = '2026-03-04T21:05:00+00:00'
const T0 = Date.parse(AS_OF)

describe('snapshot freshness', () => {
  it('measures age from as_of', () => {
    expect(ageMs(AS_OF, T0 + 42_000)).toBe(42_000)
    expect(ageMs(AS_OF, T0 - 5_000)).toBe(0) // clock skew never goes negative
    expect(ageMs('not a date', T0)).toBeNull()
  })

  it('is fresh up to and including 3 minutes, stale after', () => {
    expect(FRESHNESS.staleAfterMs).toBe(180_000)
    expect(freshnessOf(AS_OF, T0 + 60_000)).toBe('fresh')
    expect(freshnessOf(AS_OF, T0 + 180_000)).toBe('fresh')
    expect(freshnessOf(AS_OF, T0 + 180_001)).toBe('stale')
    expect(freshnessOf(AS_OF, T0 + 12 * 60_000)).toBe('stale')
    expect(freshnessOf(undefined, T0)).toBe('unknown')
  })

  it('burns the fuse down across the window', () => {
    expect(fuseRemaining(AS_OF, T0)).toBe(1)
    expect(fuseRemaining(AS_OF, T0 + 90_000)).toBeCloseTo(0.5)
    expect(fuseRemaining(AS_OF, T0 + 10 * 60_000)).toBe(0)
  })
})

describe('page freshness (F9: no false outage alert on returning to a tab)', () => {
  const old = T0 + 6 * 60_000 // the snapshot is 6 minutes old here
  const visible = { hidden: false, resumedAt: null }

  it('trusts the age while the page has been polling', () => {
    expect(pageFreshness(AS_OF, old, { ...visible, answeredAt: old - 5_000 })).toBe('stale')
    expect(pageFreshness(AS_OF, T0 + 60_000, { ...visible, answeredAt: T0 + 55_000 })).toBe('fresh')
    expect(pageFreshness(undefined, old, { ...visible, answeredAt: 0 })).toBe('unknown')
  })

  it('is only checking while hidden and not polled for longer than one poll', () => {
    expect(pageFreshness(AS_OF, old, { hidden: true, resumedAt: null, answeredAt: old - 2 * 60_000 })).toBe('checking')
    expect(pageFreshness(AS_OF, old, { hidden: true, resumedAt: null, answeredAt: old - 10_000 })).toBe('stale')
  })

  it('is checking just after a return, until the refetch answers or the grace runs out', () => {
    const resumedAt = old - 2_000
    const polling = { hidden: false, resumedAt, answeredAt: old - 5 * 60_000 }
    expect(pageFreshness(AS_OF, old, polling)).toBe('checking')
    // The refetch answered with the same old snapshot: the box really is quiet.
    expect(pageFreshness(AS_OF, old, { ...polling, answeredAt: resumedAt + 300 })).toBe('stale')
    // No answer within the grace: say so.
    expect(pageFreshness(AS_OF, resumedAt + RESUME_CHECK_MS, polling)).toBe('stale')
  })

  it('does not hide a standing alert after a short trip away', () => {
    // Hidden for 20 s after a poll: no poll was missed, so the age is real.
    expect(pageFreshness(AS_OF, old, { hidden: false, resumedAt: old - 1_000, answeredAt: old - 21_000 })).toBe('stale')
  })
})
