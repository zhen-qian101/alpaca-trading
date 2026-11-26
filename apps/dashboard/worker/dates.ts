/**
 * Calendar helpers. Session dates are US/Eastern trading dates (contract
 * IsoDate), so "today" is always today in America/New_York, whatever zone the
 * Worker or the viewer is in.
 */
import { APPROVAL_WINDOW_DAYS, ISO_DATE_RE, type IsoDate, type IsoUtc } from '../src/contract.ts'

const DAY_MS = 86_400_000

const EASTERN = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

/** The calendar date in America/New_York at `now`. */
export function easternDate(now: Date): IsoDate {
  const parts: Record<string, string> = {}
  for (const part of EASTERN.formatToParts(now)) parts[part.type] = part.value
  return `${parts.year}-${parts.month}-${parts.day}`
}

/**
 * Days since 1970-01-01 for a REAL calendar date "YYYY-MM-DD", or null when
 * the string is malformed or names a day that does not exist (2026-02-30).
 */
export function dayNumber(date: string): number | null {
  if (!ISO_DATE_RE.test(date)) return null
  const [y, m, d] = date.split('-').map(Number)
  const t = Date.UTC(y, m - 1, d)
  const back = new Date(t)
  if (back.getUTCFullYear() !== y || back.getUTCMonth() !== m - 1 || back.getUTCDate() !== d) return null
  return t / DAY_MS
}

export function isCalendarDate(date: string): boolean {
  return dayNumber(date) !== null
}

function fromDayNumber(day: number): IsoDate {
  return new Date(day * DAY_MS).toISOString().slice(0, 10)
}

/** Approvals are accepted for [ET today - before, ET today + after], inclusive. */
export function approvalWindow(now: Date): { earliest: IsoDate; latest: IsoDate } {
  const today = dayNumber(easternDate(now))
  if (today === null) throw new Error('unreachable: Intl produced a malformed date')
  return {
    earliest: fromDayNumber(today - APPROVAL_WINDOW_DAYS.before),
    latest: fromDayNumber(today + APPROVAL_WINDOW_DAYS.after),
  }
}

/** `date` must already be a calendar date (see dayNumber). */
export function inApprovalWindow(date: IsoDate, now: Date): boolean {
  const { earliest, latest } = approvalWindow(now)
  // Fixed-width YYYY-MM-DD strings compare in calendar order.
  return date >= earliest && date <= latest
}

/** ISO-8601 UTC with an explicit "+00:00" offset and whole seconds, as Python writes it. */
export function isoUtc(now: Date): IsoUtc {
  return now.toISOString().replace(/\.\d{3}Z$/, '+00:00')
}
