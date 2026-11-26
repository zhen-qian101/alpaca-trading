/** Small derived figures the pages show. Pure, so they are tested directly. */
import type { Account, Position, RoundTrip, SignalRow, Trades } from '../data/portfolio.ts'
import { dateToEpoch, etIsoDate, timestampToEpoch } from './format.ts'

const DAY_MS = 86_400_000

/**
 * Calendar days the account has been trading, counting the first day:
 * started Jan 5 and as of Jan 5 is day 1. Null when either end is unknown.
 */
export function daysLive(startDate: string | null, asOf: string): number | null {
  if (!startDate) return null
  const start = dateToEpoch(startDate)
  const asOfMs = timestampToEpoch(asOf)
  if (start === null || asOfMs === null) return null
  const end = dateToEpoch(etIsoDate(asOfMs))
  if (end === null || end < start) return null
  return Math.round((end - start) / DAY_MS) + 1
}

export interface CurveSummary {
  first: { date: string; equity: number }
  last: { date: string; equity: number }
  high: { date: string; equity: number }
  low: { date: string; equity: number }
}

/** First, last, highest and lowest close on the daily curve. */
export function summarizeCurve(daily: Account['daily']): CurveSummary | null {
  const first = daily[0]
  const last = daily.at(-1)
  if (!first || !last) return null
  let high = first
  let low = first
  for (const p of daily) {
    if (p.equity > high.equity) high = p
    if (p.equity < low.equity) low = p
  }
  return { first, last, high, low }
}

export interface Bin {
  /** Lower edge, as a fraction (inclusive). */
  from: number
  /** Upper edge, as a fraction (exclusive, except the last bin). */
  to: number
  count: number
}

const NICE_WIDTHS = [0.0025, 0.005, 0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5, 1]

/**
 * Histogram of round-trip returns. Bin edges sit on multiples of a "nice"
 * width, so zero is always an edge and no bin mixes wins with losses.
 * Aims for about `target` bins across the observed range.
 */
export function returnHistogram(values: readonly number[], target = 12): { width: number; bins: Bin[] } {
  const finite = values.filter(Number.isFinite)
  if (!finite.length) return { width: 0, bins: [] }
  const lo = Math.min(...finite)
  const hi = Math.max(...finite)
  const span = Math.max(hi - lo, 1e-9)
  const width = NICE_WIDTHS.find((w) => span / w <= target) ?? Math.ceil((span / target) * 100) / 100
  // Integer bin indices avoid floating drift at the edges. The bins either
  // side of zero are always present, so the zero line has a place on the axis.
  const index = (v: number) => Math.floor(v / width + 1e-9)
  const firstIdx = Math.min(index(lo), -1)
  const lastIdx = Math.max(index(hi), 0)
  const bins: Bin[] = []
  for (let i = firstIdx; i <= lastIdx; i++) bins.push({ from: i * width, to: (i + 1) * width, count: 0 })
  for (const v of finite) {
    const bin = bins[index(v) - firstIdx]
    if (bin) bin.count++
  }
  return { width, bins }
}

export type TradeSortKey = 'symbol' | 'entry_at' | 'exit_at' | 'held' | 'pnl' | 'pnl_pct'
export type SortDir = 'asc' | 'desc'

function heldMs(t: RoundTrip): number {
  const a = timestampToEpoch(t.entry_at)
  const b = timestampToEpoch(t.exit_at)
  return a === null || b === null ? 0 : b - a
}

export function sortTrades(trips: readonly RoundTrip[], key: TradeSortKey, dir: SortDir): RoundTrip[] {
  const sign = dir === 'asc' ? 1 : -1
  const value = (t: RoundTrip): number | string => {
    switch (key) {
      case 'symbol':
        return t.symbol
      case 'entry_at':
        return timestampToEpoch(t.entry_at) ?? 0
      case 'exit_at':
        return timestampToEpoch(t.exit_at) ?? 0
      case 'held':
        return heldMs(t)
      case 'pnl':
        return t.pnl
      case 'pnl_pct':
        return t.pnl_pct
    }
  }
  // Ties fall back to the newest exit first, the file's own order.
  return trips
    .map((t, i) => ({ t, i }))
    .sort((a, b) => {
      const va = value(a.t)
      const vb = value(b.t)
      if (va < vb) return -sign
      if (va > vb) return sign
      return a.i - b.i
    })
    .map(({ t }) => t)
}

const BIAS_ORDER = { long: 0, short: 1, flat: 2 } as const

/** Longs first by priority (highest first), then the rest by symbol. */
export function sortSignals(rows: readonly SignalRow[]): SignalRow[] {
  return [...rows].sort((a, b) => {
    const ba = a.bias === null ? 3 : BIAS_ORDER[a.bias]
    const bb = b.bias === null ? 3 : BIAS_ORDER[b.bias]
    if (ba !== bb) return ba - bb
    const pa = a.priority ?? -Infinity
    const pb = b.priority ?? -Infinity
    if (pa !== pb) return pb - pa
    return a.symbol.localeCompare(b.symbol)
  })
}

/** Contingencies still in force at `nowMs` (the exporter drops expired ones; the page re-checks). */
export function liveContingencies(row: SignalRow, nowMs: number): SignalRow['contingencies'] {
  return row.contingencies.filter((c) => (timestampToEpoch(c.valid_until) ?? 0) > nowMs)
}

/**
 * The share of the whole account a buy really uses. The engine sizes a buy as
 * `deploy` of the share book, and the share book is equity less the options
 * sleeve's reserve (sigengine.engine.fire_event), so 97% of a book that keeps
 * 20% back is about 78% of equity.
 */
export function shareOfEquity(deploy: number | null, sleeveReserve: number | null | undefined): number | null {
  if (deploy == null || !Number.isFinite(deploy)) return null
  const reserve = sleeveReserve != null && Number.isFinite(sleeveReserve) ? sleeveReserve : 0
  return deploy * (1 - reserve)
}

/**
 * The account's own stop level for a long position: the price at which the
 * stop sells it (entry less the stop percentage). Null for a short or when
 * either figure is missing; the book never sets a stop on a short.
 */
export function stopLevel(position: Pick<Position, 'side' | 'avg_entry_price' | 'stop_pct'>): number | null {
  if (position.side !== 'long' || position.stop_pct == null) return null
  return position.avg_entry_price * (1 - position.stop_pct)
}

/**
 * The deepest return on a closed round trip, or null when none has lost. The
 * risk limits quote it: a stop is checked at a bar's close, so a loss can run
 * well past it.
 */
export function worstTrip(trades: Pick<Trades, 'round_trips'> | null | undefined): number | null {
  const losses = (trades?.round_trips ?? []).map((t) => t.pnl_pct).filter((v) => v < 0)
  return losses.length ? Math.min(...losses) : null
}
