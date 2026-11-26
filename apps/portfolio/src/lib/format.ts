/**
 * Number and date formatting for the public pages.
 *
 * Ledger conventions: a signed figure always carries its sign, and the minus
 * is a true minus (U+2212), the width of the plus, so columns of signed
 * figures line up. Gains are set in ink and losses in red ink (see `tone`),
 * but the sign is always there too: colour never carries meaning alone.
 *
 * Every *_pct in portfolio.json is a fraction (0.0123 = 1.23%).
 */

export const MINUS = '−'
export const DASH = '–'
const ZONE = 'America/New_York'

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })
const usdWhole = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })

/** Replace the ASCII hyphen-minus Intl uses with a true minus. */
const trueMinus = (s: string) => s.replace(/^-/, MINUS)

/** Rounds to 0 at the shown precision, so "−$0.00" and "−0.00%" never appear. */
function roundsToZero(value: number, digits: number): boolean {
  return Math.abs(value) < 0.5 * 10 ** -digits
}

export function money(value: number | null | undefined, { whole = false } = {}): string {
  if (value == null || !Number.isFinite(value)) return DASH
  const digits = whole ? 0 : 2
  if (roundsToZero(value, digits)) return (whole ? usdWhole : usd).format(0)
  return trueMinus((whole ? usdWhole : usd).format(value))
}

export function signedMoney(value: number | null | undefined, opts: { whole?: boolean } = {}): string {
  if (value == null || !Number.isFinite(value)) return DASH
  const digits = opts.whole ? 0 : 2
  if (roundsToZero(value, digits)) return money(0, opts)
  const body = money(Math.abs(value), opts)
  return value > 0 ? `+${body}` : `${MINUS}${body}`
}

/** A fraction as a percentage: 0.0383 -> "3.83%". */
export function pct(fraction: number | null | undefined, digits = 2): string {
  if (fraction == null || !Number.isFinite(fraction)) return DASH
  const value = fraction * 100
  if (roundsToZero(value, digits)) return `${(0).toFixed(digits)}%`
  return trueMinus(`${value.toFixed(digits)}%`)
}

/** A fraction as a percentage with no trailing zeros: 0.02 -> "2%", 0.025 -> "2.5%". */
export function pctShort(fraction: number | null | undefined): string {
  if (fraction == null || !Number.isFinite(fraction)) return DASH
  const value = Math.round(fraction * 10000) / 100
  return trueMinus(`${new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(value)}%`)
}

/**
 * A fraction as a signed percentage with no trailing zeros, to at most two
 * places: 0.0025 -> "+0.25%", -0.005 -> "−0.5%", 0 -> "0%". For values that
 * sit on a grid (histogram edges), where rounding to a tenth would misplace
 * them.
 */
export function signedPctShort(fraction: number | null | undefined): string {
  if (fraction == null || !Number.isFinite(fraction)) return DASH
  const value = Math.round(fraction * 10000) / 100
  if (value === 0) return '0%'
  const body = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(Math.abs(value))
  return `${value > 0 ? '+' : MINUS}${body}%`
}

/** A fraction as a signed percentage: 0.0383 -> "+3.83%", -0.0823 -> "−8.23%". */
export function signedPct(fraction: number | null | undefined, digits = 2): string {
  if (fraction == null || !Number.isFinite(fraction)) return DASH
  const value = fraction * 100
  if (roundsToZero(value, digits)) return `${(0).toFixed(digits)}%`
  const body = Math.abs(value).toFixed(digits)
  return value > 0 ? `+${body}%` : `${MINUS}${body}%`
}

/** A fill price: cents for most things, more places for a sub-dollar fill. */
export function price(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH
  const digits = Math.abs(value) < 1 ? 4 : 2
  return trueMinus(
    new Intl.NumberFormat('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value),
  )
}

/** Shares or coins: whole numbers plainly, fractions to at most 6 places. */
export function quantity(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 6 }).format(value)
}

export function decimal(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return DASH
  return trueMinus(value.toFixed(digits))
}

/** A strategy parameter: 12 -> "12", 1.5 -> "1.5". */
export function param(value: number | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 3 }).format(value)
}

export type Tone = 'gain' | 'loss' | 'even'

export function tone(value: number | null | undefined): Tone {
  if (value == null || !Number.isFinite(value) || value === 0) return 'even'
  return value > 0 ? 'gain' : 'loss'
}

// --------------------------------------------------------------------- dates

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/

/** A session date "YYYY-MM-DD" as a UTC-midnight epoch (ms), or null. */
export function dateToEpoch(date: string): number | null {
  const m = DATE_RE.exec(date)
  if (!m) return null
  const [, y, mo, d] = m
  const ms = Date.UTC(Number(y), Number(mo) - 1, Number(d))
  const back = new Date(ms)
  // Reject 2026-02-31 and friends instead of rolling them over.
  if (back.getUTCMonth() !== Number(mo) - 1 || back.getUTCDate() !== Number(d)) return null
  return ms
}

const dayFmt = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric', year: 'numeric' })
const dayShortFmt = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' })

/** "2026-06-12" -> "Jun 12, 2026". Session dates carry no zone; shown as written. */
export function longDate(date: string | null | undefined): string {
  if (!date) return DASH
  const ms = dateToEpoch(date)
  return ms === null ? date : dayFmt.format(ms)
}

/** "2026-06-12" -> "Jun 12". */
export function shortDate(date: string | null | undefined): string {
  if (!date) return DASH
  const ms = dateToEpoch(date)
  return ms === null ? date : dayShortFmt.format(ms)
}

export function timestampToEpoch(ts: string): number | null {
  const ms = Date.parse(ts)
  return Number.isFinite(ms) ? ms : null
}

const etDateTimeFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: ZONE,
  month: 'short',
  day: 'numeric',
  year: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
})
const etShortFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: ZONE,
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
})
const etTimeFmt = new Intl.DateTimeFormat('en-US', { timeZone: ZONE, hour: 'numeric', minute: '2-digit' })
const etDateFmt = new Intl.DateTimeFormat('en-US', { timeZone: ZONE, month: 'short', day: 'numeric', year: 'numeric' })
const etIsoParts = new Intl.DateTimeFormat('en-US', {
  timeZone: ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
})

/** A UTC timestamp in New York time: "Jun 12, 2026, 6:05 PM ET". */
export function etDateTime(ts: string | null | undefined): string {
  if (!ts) return DASH
  const ms = timestampToEpoch(ts)
  return ms === null ? ts : `${etDateTimeFmt.format(ms)} ET`
}

/** "Jun 12, 6:05 PM ET". */
export function etShortDateTime(ts: string | null | undefined): string {
  if (!ts) return DASH
  const ms = timestampToEpoch(ts)
  return ms === null ? ts : `${etShortFmt.format(ms)} ET`
}

/** "Jun 12, 2026" for a UTC timestamp, by its New York date. */
export function etDate(ts: string | null | undefined): string {
  if (!ts) return DASH
  const ms = timestampToEpoch(ts)
  return ms === null ? ts : etDateFmt.format(ms)
}

/** "6:05 PM" in New York time, for an epoch in ms. */
export function etTime(ms: number): string {
  return etTimeFmt.format(ms)
}

/** The New York calendar date of an instant, as "YYYY-MM-DD". */
export function etIsoDate(ms: number): string {
  const p = Object.fromEntries(etIsoParts.formatToParts(ms).map((x) => [x.type, x.value]))
  return `${p.year}-${p.month}-${p.day}`
}

/**
 * New York's offset from UTC at an instant, in seconds (−14400 in summer,
 * −18000 in winter). Charts label time in UTC, so ET wall-clock labels come
 * from shifting each point by this.
 */
export function etOffsetSeconds(ms: number): number {
  const p = Object.fromEntries(etIsoParts.formatToParts(ms).map((x) => [x.type, x.value]))
  const asUtc = Date.UTC(
    Number(p.year),
    Number(p.month) - 1,
    Number(p.day),
    Number(p.hour) % 24,
    Number(p.minute),
    Number(p.second),
  )
  return Math.round((asUtc - Math.floor(ms / 1000) * 1000) / 1000)
}

/** A video length: 65 -> "1:05", 3725 -> "1:02:05". */
export function clock(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return DASH
  const s = Math.round(seconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = String(s % 60).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`
}

/** A holding period: "45m", "4h 15m", "3d 2h". */
export function holding(fromTs: string, toTs: string): string {
  const a = timestampToEpoch(fromTs)
  const b = timestampToEpoch(toTs)
  if (a === null || b === null || b < a) return DASH
  const minutes = Math.round((b - a) / 60000)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) {
    const rest = minutes % 60
    return rest ? `${hours}h ${rest}m` : `${hours}h`
  }
  const days = Math.floor(hours / 24)
  const restHours = hours % 24
  return restHours ? `${days}d ${restHours}h` : `${days}d`
}

/** Spoken form of a video length for screen readers: "1 minute 5 seconds". */
export function spokenDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return ''
  const s = Math.round(seconds)
  const m = Math.floor(s / 60)
  const r = s % 60
  const parts = []
  if (m) parts.push(`${m} minute${m === 1 ? '' : 's'}`)
  if (r || !m) parts.push(`${r} second${r === 1 ? '' : 's'}`)
  return parts.join(' ')
}
