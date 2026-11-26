/**
 * Pure helpers for the signal chart: keeping the viewer's zoom/pan across
 * refreshes, axis labels in an explicit zone, and theme-aware marker colours.
 */
import type { ChartMarker, EpochSeconds } from '../../../contract.ts'
import {
  formatClock,
  formatDayMonth,
  formatLongDateTime,
  formatMonth,
  formatYear,
  ZONES,
  type ZoneKey,
} from '../../../lib/time.ts'

export interface LogicalRange {
  from: number
  to: number
}

/** Bars to show on first draw: about 7 px per bar, at least 40, at most all of them. */
export function defaultBarCount(widthPx: number, total: number): number {
  const fit = Math.floor(widthPx / 7)
  return Math.max(1, Math.min(total, Math.max(40, fit)))
}

/** The default window: the newest `bars` bars with a little room on the right. */
export function defaultRange(total: number, bars: number): LogicalRange {
  return { from: total - bars - 0.5, to: total - 1 + 3 }
}

/** Index of the first time >= t (times ascending); times.length when none. */
export function lowerBound(times: readonly number[], t: number): number {
  let lo = 0
  let hi = times.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (times[mid] < t) lo = mid + 1
    else hi = mid
  }
  return lo
}

/**
 * Where the visible window should sit after the series is replaced by a
 * refreshed one, so a refresh never undoes the viewer's zoom or pan.
 *
 *   - Looking at the newest bar: stay pinned to the right edge with the same
 *     zoom (span) and right-hand padding, so a new bar scrolls into view.
 *   - Panned into history: keep the same BARS in view. The publisher keeps a
 *     fixed-length window (oldest bar drops as a new one arrives), so logical
 *     indices shift; the left-edge bar's time is the anchor.
 *   - No previous view: null (the caller applies the default).
 */
export function planVisibleRange(
  prev: LogicalRange | null,
  prevTimes: readonly EpochSeconds[],
  nextTimes: readonly EpochSeconds[],
): LogicalRange | null {
  if (!prev || prevTimes.length === 0 || nextTimes.length === 0) return null
  const prevLast = prevTimes.length - 1
  const nextLast = nextTimes.length - 1
  const span = prev.to - prev.from
  if (prev.to >= prevLast - 0.5) {
    const rightPad = prev.to - prevLast
    const to = nextLast + rightPad
    return { from: to - span, to }
  }
  const anchor = Math.min(prevLast, Math.max(0, Math.round(prev.from)))
  const shift = lowerBound(nextTimes, prevTimes[anchor]) - anchor
  return { from: prev.from + shift, to: prev.to + shift }
}

/** Tick-mark kinds, mirroring lightweight-charts' TickMarkType enum values. */
export const TICK = { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 } as const

/** Time-axis label for a TRUE UTC epoch, in the chosen zone. */
export function tickLabel(time: EpochSeconds, tickType: number, zone: ZoneKey): string {
  switch (tickType) {
    case TICK.Year:
      return formatYear(time, zone)
    case TICK.Month:
      return formatMonth(time, zone)
    case TICK.DayOfMonth:
      return formatDayMonth(time, zone)
    default:
      return formatClock(time, zone)
  }
}

/** Crosshair label: "Wed 4 Mar 2026 15:00 ET". */
export function crosshairLabel(time: EpochSeconds, zone: ZoneKey): string {
  return `${formatLongDateTime(time, zone)} ${ZONES[zone].label}`
}

export interface MarkerPalette {
  up: string
  down: string
  warn: string
  fillBuy: string
  fillSell: string
}

/**
 * The publisher's markers carry fixed hex colours chosen for a dark
 * background. Map the known families onto the theme's tokens so they keep
 * their contrast in the light theme; anything unrecognised passes through.
 */
export function themeMarkerColor(color: string, palette: MarkerPalette): string {
  switch (color.toLowerCase()) {
    case '#34b892':
      return palette.up
    case '#e2596b':
      return palette.down
    case '#f2a93b':
      return palette.warn
    case '#7b8fff':
      return palette.fillBuy
    case '#c58fff':
      return palette.fillSell
    default:
      return color
  }
}

export type MarkerFamily = 'signal-entry' | 'signal-exit' | 'account-exit' | 'fill-buy' | 'fill-sell' | 'other'

/** Which of the three marker families (contract ChartMarker) a marker belongs to. */
export function markerFamily(marker: ChartMarker): MarkerFamily {
  if (marker.shape === 'circle') return marker.position === 'belowBar' ? 'fill-buy' : 'fill-sell'
  if (marker.color.toLowerCase() === '#f2a93b') return 'account-exit'
  if (marker.shape === 'arrowUp') return 'signal-entry'
  if (marker.shape === 'arrowDown') return 'signal-exit'
  return 'other'
}

export const MARKER_FAMILY_LABEL: Record<MarkerFamily, string> = {
  'signal-entry': 'Strategy entry signal',
  'signal-exit': 'Strategy exit signal',
  'account-exit': 'Account exit (rotation or reconcile)',
  'fill-buy': 'Broker fill, buy',
  'fill-sell': 'Broker fill, sell',
  other: 'Marker',
}
