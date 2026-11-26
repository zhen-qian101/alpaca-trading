import { describe, expect, it } from 'vitest'

import { chartFixtures } from '../../../test/fixtures.ts'
import { markerFamily, planVisibleRange, themeMarkerColor } from './chartMath.ts'

const times = (from: number, n: number) => Array.from({ length: n }, (_, i) => from + i * 3600)

describe('keeping the zoom/pan across a refresh', () => {
  const prev = times(1_000_000, 220)

  it('stays pinned to the newest bar, same zoom, when a bar is added', () => {
    // Window slides: the oldest bar drops, one new bar arrives.
    const next = times(1_000_000 + 3600, 220)
    expect(planVisibleRange({ from: 159.5, to: 222 }, prev, next)).toEqual({ from: 159.5, to: 222 })
    // A longer series (no drop) moves the window right by one bar.
    const grown = times(1_000_000, 221)
    expect(planVisibleRange({ from: 159.5, to: 222 }, prev, grown)).toEqual({ from: 160.5, to: 223 })
  })

  it('keeps the same bars in view when panned into history', () => {
    const next = times(1_000_000 + 3600, 220) // indices shift left by one
    expect(planVisibleRange({ from: 50, to: 100 }, prev, next)).toEqual({ from: 49, to: 99 })
    expect(planVisibleRange({ from: 50, to: 100 }, prev, prev)).toEqual({ from: 50, to: 100 })
  })

  it('has no opinion on a first draw', () => {
    expect(planVisibleRange(null, [], prev)).toBeNull()
  })
})

describe('markers', () => {
  const palette = { up: 'UP', down: 'DOWN', warn: 'WARN', fillBuy: 'FB', fillSell: 'FS' }

  it('maps the publisher colours onto theme tokens', () => {
    expect(themeMarkerColor('#34b892', palette)).toBe('UP')
    expect(themeMarkerColor('#E2596B', palette)).toBe('DOWN')
    expect(themeMarkerColor('#f2a93b', palette)).toBe('WARN')
    expect(themeMarkerColor('#123456', palette)).toBe('#123456')
  })

  it('tells the three families apart', () => {
    const families = chartFixtures.ACME.symbols.ACME.markers.map(markerFamily)
    expect(families).toContain('account-exit')
    expect(families).toContain('signal-entry')
    expect(families).toContain('fill-buy')
  })
})
