import { describe, expect, it } from 'vitest'

import fixture from '../../fixtures/portfolio.json'
import { parsePortfolio, type Portfolio, type SignalRow } from '../data/portfolio.ts'
import {
  daysLive,
  liveContingencies,
  returnHistogram,
  shareOfEquity,
  sortSignals,
  sortTrades,
  stopLevel,
  summarizeCurve,
  worstTrip,
} from './stats.ts'

const data = (() => {
  const r = parsePortfolio(fixture)
  if (!r.ok) throw new Error('fixture invalid')
  return r.value as Portfolio
})()

describe('daysLive', () => {
  it('counts calendar days, the first day included, by the New York date', () => {
    expect(daysLive('2026-01-05', '2026-01-05T20:00:00Z')).toBe(1)
    expect(daysLive('2026-01-05', '2026-06-12T22:05:00Z')).toBe(159)
    // 01:00 UTC on Jan 6 is still Jan 5 in New York.
    expect(daysLive('2026-01-05', '2026-01-06T01:00:00Z')).toBe(1)
  })

  it('is unknown without a start', () => {
    expect(daysLive(null, '2026-06-12T22:05:00Z')).toBeNull()
  })
})

describe('summarizeCurve', () => {
  it('finds the first, last, highest and lowest close', () => {
    const s = summarizeCurve(data.account!.daily)!
    expect(s.first.date).toBe('2026-01-05')
    expect(s.last.equity).toBe(data.account!.equity)
    expect(s.high.equity).toBeGreaterThanOrEqual(s.last.equity)
    expect(s.low.equity).toBeLessThanOrEqual(s.first.equity)
  })

  it('is null for an empty curve', () => {
    expect(summarizeCurve([])).toBeNull()
  })
})

describe('returnHistogram', () => {
  it('puts every value in exactly one bin and keeps zero on an edge', () => {
    const values = data.trades!.round_trips.map((t) => t.pnl_pct)
    const { width, bins } = returnHistogram(values)
    expect(bins.reduce((n, b) => n + b.count, 0)).toBe(values.length)
    expect(bins.some((b) => Math.abs(b.from) < 1e-12)).toBe(true)
    for (const b of bins) {
      expect(b.to - b.from).toBeCloseTo(width)
      // No bin straddles zero, so a bin is all wins or all losses.
      expect(b.from >= -1e-12 || b.to <= 1e-12).toBe(true)
    }
  })

  it('handles one value and no values', () => {
    expect(returnHistogram([]).bins).toEqual([])
    const one = returnHistogram([0.01])
    expect(one.bins.reduce((n, b) => n + b.count, 0)).toBe(1)
  })

  it('stays small for a wild range', () => {
    expect(returnHistogram([-900, 900]).bins.length).toBeLessThan(20)
  })
})

describe('sorting', () => {
  it('sorts trades by any sortable column, ties in file order', () => {
    const trips = data.trades!.round_trips
    const byPnl = sortTrades(trips, 'pnl', 'desc')
    expect(byPnl[0]!.pnl).toBe(Math.max(...trips.map((t) => t.pnl)))
    const bySymbol = sortTrades(trips, 'symbol', 'asc').map((t) => t.symbol)
    expect(bySymbol).toEqual([...bySymbol].sort())
    expect(sortTrades(trips, 'exit_at', 'desc')).toEqual(trips)
  })

  it('lists longs first by priority', () => {
    const rows = sortSignals(data.signals!.rows)
    expect(rows.slice(0, 3).map((r) => r.symbol)).toEqual(['ACME', 'NOVA', 'LUMA'])
    expect(rows.at(-1)!.bias).toBe('flat')
  })

  it('drops standing orders that have expired', () => {
    const row: SignalRow = {
      symbol: 'ORBX',
      bias: 'flat',
      priority: 0,
      suggested_size: 0,
      contingencies: [
        { condition: 'close_above', level: 61.5, valid_until: '2026-06-16T04:00:00Z' },
        { condition: 'close_below', level: 50, valid_until: '2026-06-10T04:00:00Z' },
      ],
    }
    expect(liveContingencies(row, Date.parse('2026-06-12T22:05:00Z'))).toHaveLength(1)
    expect(liveContingencies(row, Date.parse('2026-06-20T00:00:00Z'))).toHaveLength(0)
  })
})

describe('shareOfEquity', () => {
  it('takes the options sleeve’s reserve off before the deploy fraction applies', () => {
    expect(shareOfEquity(0.97, 0.2)).toBeCloseTo(0.776, 10)
    expect(shareOfEquity(0.97, 0)).toBe(0.97)
    expect(shareOfEquity(0.97, null)).toBe(0.97)
    expect(shareOfEquity(null, 0.2)).toBeNull()
  })
})

describe('stopLevel', () => {
  it('is the entry less the stop percentage, for a long only', () => {
    expect(stopLevel({ side: 'long', avg_entry_price: 250.5, stop_pct: 0.02 })).toBeCloseTo(245.49, 10)
    expect(stopLevel({ side: 'short', avg_entry_price: 250.5, stop_pct: 0.02 })).toBeNull()
    expect(stopLevel({ side: 'long', avg_entry_price: 250.5, stop_pct: null })).toBeNull()
  })
})

describe('worstTrip', () => {
  it('is the deepest loss on a closed round trip', () => {
    const trips = data.trades!.round_trips
    expect(worstTrip(data.trades)).toBe(Math.min(...trips.map((t) => t.pnl_pct)))
    expect(worstTrip(data.trades)).toBeLessThan(0)
  })

  it('is null with no losing trip, no trips or no trades section', () => {
    const wins = data.trades!.round_trips.filter((t) => t.pnl_pct >= 0)
    expect(worstTrip({ round_trips: wins })).toBeNull()
    expect(worstTrip({ round_trips: [] })).toBeNull()
    expect(worstTrip(null)).toBeNull()
  })
})
