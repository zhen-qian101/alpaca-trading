/**
 * Every timestamp the dashboard shows is a TRUE UTC instant formatted in an
 * explicit zone. Nothing here reads the browser's own zone or the UTC getters
 * as wall clock.
 */
import type { EpochSeconds, IsoDate } from '../contract.ts'

export const ZONES = {
  et: { id: 'America/New_York', label: 'ET', long: 'US Eastern' },
  melbourne: { id: 'Australia/Melbourne', label: 'Melbourne', long: 'Melbourne' },
} as const

export type ZoneKey = keyof typeof ZONES
export const DEFAULT_ZONE: ZoneKey = 'et'

export function isZoneKey(value: unknown): value is ZoneKey {
  return value === 'et' || value === 'melbourne'
}

// Intl.DateTimeFormat construction is comparatively slow and these run per
// table cell and per chart tick, so formatters are cached by zone + shape.
const formatterCache = new Map<string, Intl.DateTimeFormat>()
function formatter(zone: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = zone + JSON.stringify(options)
  let f = formatterCache.get(key)
  if (!f) {
    f = new Intl.DateTimeFormat('en-GB', { timeZone: zone, hourCycle: 'h23', ...options })
    formatterCache.set(key, f)
  }
  return f
}

type Parts = Record<'year' | 'month' | 'day' | 'hour' | 'minute' | 'second' | 'weekday', string>

function parts(epochMs: number, zone: string): Parts {
  const out = {} as Parts
  const f = formatter(zone, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    weekday: 'short',
  })
  for (const p of f.formatToParts(new Date(epochMs))) {
    if (p.type in { year: 1, month: 1, day: 1, hour: 1, minute: 1, second: 1, weekday: 1 }) {
      out[p.type as keyof Parts] = p.value
    }
  }
  return out
}

/** "4 Mar 15:00" in the zone. */
export function formatDateTime(epoch: EpochSeconds | null | undefined, zone: ZoneKey): string {
  if (epoch == null || !Number.isFinite(epoch)) return '—'
  const p = parts(epoch * 1000, ZONES[zone].id)
  return `${p.day} ${p.month} ${p.hour}:${p.minute}`
}

/** "Wed 4 Mar 2026 15:00" in the zone (chart crosshair, tooltips). */
export function formatLongDateTime(epoch: EpochSeconds | null | undefined, zone: ZoneKey): string {
  if (epoch == null || !Number.isFinite(epoch)) return '—'
  const p = parts(epoch * 1000, ZONES[zone].id)
  return `${p.weekday} ${p.day} ${p.month} ${p.year} ${p.hour}:${p.minute}`
}

/** "15:00" in the zone. */
export function formatClock(epoch: EpochSeconds | null | undefined, zone: ZoneKey): string {
  if (epoch == null || !Number.isFinite(epoch)) return '—'
  const p = parts(epoch * 1000, ZONES[zone].id)
  return `${p.hour}:${p.minute}`
}

/** "15:00:42" in the zone. */
export function formatClockSeconds(epoch: EpochSeconds | null | undefined, zone: ZoneKey): string {
  if (epoch == null || !Number.isFinite(epoch)) return '—'
  const p = parts(epoch * 1000, ZONES[zone].id)
  return `${p.hour}:${p.minute}:${p.second}`
}

/** "4 Mar" in the zone. */
export function formatDayMonth(epoch: EpochSeconds, zone: ZoneKey): string {
  const p = parts(epoch * 1000, ZONES[zone].id)
  return `${p.day} ${p.month}`
}

/** "Mar" in the zone. */
export function formatMonth(epoch: EpochSeconds, zone: ZoneKey): string {
  return parts(epoch * 1000, ZONES[zone].id).month
}

/** "2026" in the zone. */
export function formatYear(epoch: EpochSeconds, zone: ZoneKey): string {
  return parts(epoch * 1000, ZONES[zone].id).year
}

/** "Wed 4 Mar 2026" for a calendar date (no zone shift: it is already a date). */
export function formatIsoDate(date: IsoDate | null | undefined): string {
  if (!date) return '—'
  const [y, m, d] = date.split('-').map(Number)
  if (!y || !m || !d) return date
  const f = formatter('UTC', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })
  return f.format(new Date(Date.UTC(y, m - 1, d, 12)))
}

/** Parse an ISO-8601 timestamp to epoch seconds; null when unparseable. */
export function isoToEpoch(iso: string | null | undefined): EpochSeconds | null {
  if (!iso) return null
  const ms = Date.parse(iso)
  return Number.isFinite(ms) ? ms / 1000 : null
}

/** Compact age: "42 s", "12 min", "5 h", "3 d". */
export function formatAge(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 90) return `${s} s`
  const min = Math.round(s / 60)
  if (min < 90) return `${min} min`
  const h = Math.round(s / 3600)
  if (h < 48) return `${h} h`
  return `${Math.round(s / 86400)} d`
}

/** "42 s ago" from an epoch, relative to `nowMs`. */
export function formatAgo(epoch: EpochSeconds | null | undefined, nowMs: number): string {
  if (epoch == null || !Number.isFinite(epoch)) return '—'
  return `${formatAge(nowMs - epoch * 1000)} ago`
}

/** Countdown: "3 h 12 min", "12 min", "45 s". */
export function formatCountdown(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  if (s < 60) return `${s} s`
  const totalMin = Math.floor(s / 60)
  const h = Math.floor(totalMin / 60)
  const min = totalMin % 60
  return h > 0 ? `${h} h ${String(min).padStart(2, '0')} min` : `${min} min`
}

/** The calendar date at `epochMs` in a zone, "YYYY-MM-DD". */
export function dateInZone(epochMs: number, zoneId: string): IsoDate {
  // en-CA formats as YYYY-MM-DD.
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: zoneId,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date(epochMs))
}

/** Today's US/Eastern date: the trading session date. */
export function etToday(nowMs: number): IsoDate {
  return dateInZone(nowMs, ZONES.et.id)
}

/** The zone's wall clock at `epochMs`, to the second, with its fields read back as if they were UTC. */
function wallClockSecondsAsUtc(epochMs: number, zoneId: string): number {
  const f = formatter(zoneId, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
  const p: Record<string, number> = {}
  for (const part of f.formatToParts(new Date(epochMs))) {
    if (part.type !== 'literal') p[part.type] = Number(part.value)
  }
  return Date.UTC(p.year, p.month - 1, p.day, p.hour % 24, p.minute, p.second)
}

/** Offset of `zoneId` from UTC at the instant `epochMs`, in minutes (ET in winter: -300). */
function zoneOffsetMinutes(epochMs: number, zoneId: string): number {
  return Math.round((wallClockSecondsAsUtc(epochMs, zoneId) - Math.floor(epochMs / 1000) * 1000) / 60_000)
}

/**
 * The wall-clock time of the instant `epochMs` in a zone, as milliseconds
 * whose UTC getters read that zone's calendar fields (e.g. 00:00 ET reads as
 * getUTCHours() === 0). For COMPARING calendar fields only: it is not an
 * instant, and must never be formatted or sent anywhere as one.
 */
export function wallClockMs(epochMs: number, zoneId: string): number {
  const whole = Math.floor(epochMs / 1000) * 1000
  return wallClockSecondsAsUtc(epochMs, zoneId) + (epochMs - whole)
}

/**
 * The UTC instant (ms) of a wall-clock time on a date in a zone, e.g. 20:00 ET
 * on 2026-03-04. Two passes settle the offset across a DST change.
 */
export function zonedWallTimeToUtcMs(date: IsoDate, hour: number, minute: number, zoneId: string): number {
  const [y, m, d] = date.split('-').map(Number)
  const wall = Date.UTC(y, m - 1, d, hour, minute)
  let guess = wall - zoneOffsetMinutes(wall, zoneId) * 60_000
  guess = wall - zoneOffsetMinutes(guess, zoneId) * 60_000
  return guess
}

/** Shift a "YYYY-MM-DD" by whole days. */
export function addDays(date: IsoDate, days: number): IsoDate {
  const [y, m, d] = date.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10)
}
