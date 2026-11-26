/**
 * Number formatting. Signs are always printed on changes, so direction never
 * depends on colour alone, and the minus is a real U+2212 so columns align.
 */
import type { Fraction, USD } from '../contract.ts'

const MINUS = '−'
const DASH = '—'

const moneyFormatters = new Map<number, Intl.NumberFormat>()
function moneyFormatter(dp: number): Intl.NumberFormat {
  let f = moneyFormatters.get(dp)
  if (!f) {
    f = new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: dp,
      maximumFractionDigits: dp,
    })
    moneyFormatters.set(dp, f)
  }
  return f
}

function ok(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/** "$1,234.56"; negative as "−$1,234.56". */
export function money(v: USD | null | undefined, dp = 2): string {
  if (!ok(v)) return DASH
  const s = moneyFormatter(dp).format(Math.abs(v))
  return v < 0 && Number(s.replace(/[^0-9.]/g, '')) !== 0 ? MINUS + s : s
}

/** "+$1,234.56" / "−$12.00" / "$0.00". */
export function signedMoney(v: USD | null | undefined, dp = 2): string {
  if (!ok(v)) return DASH
  const s = moneyFormatter(dp).format(Math.abs(v))
  if (Number(s.replace(/[^0-9.]/g, '')) === 0) return s
  return (v > 0 ? '+' : MINUS) + s
}

/** A plain price with no currency sign: "303.95". */
export function price(v: number | null | undefined, dp = 2): string {
  if (!ok(v)) return DASH
  const s = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp })
  return v < 0 ? MINUS + s : s
}

/** Fraction as a percentage: 0.0123 -> "1.23%". */
export function pct(v: Fraction | null | undefined, dp = 2): string {
  if (!ok(v)) return DASH
  const s = `${Math.abs(v * 100).toFixed(dp)}%`
  return v < 0 && Number((Math.abs(v) * 100).toFixed(dp)) !== 0 ? MINUS + s : s
}

/** Signed percentage: "+1.23%" / "−0.40%" / "0.00%". */
export function signedPct(v: Fraction | null | undefined, dp = 2): string {
  if (!ok(v)) return DASH
  const s = `${Math.abs(v * 100).toFixed(dp)}%`
  if (Number((Math.abs(v) * 100).toFixed(dp)) === 0) return s
  return (v > 0 ? '+' : MINUS) + s
}

/** Share / contract count: integers plain, fractions to 4 dp. */
export function qty(v: number | null | undefined): string {
  if (!ok(v)) return DASH
  const s = Number.isInteger(v) ? Math.abs(v).toLocaleString('en-US') : Math.abs(v).toFixed(4)
  return v < 0 ? MINUS + s : s
}

/** "up" / "down" / "" for colouring a change; always paired with a printed sign. */
export function direction(v: number | null | undefined): 'up' | 'down' | 'flat' {
  if (!ok(v) || v === 0) return 'flat'
  return v > 0 ? 'up' : 'down'
}

// ------------------------------------------------------------------ options

export interface OccSymbol {
  root: string
  /** "YYYY-MM-DD". */
  expiry: string
  type: 'call' | 'put'
  strike: number
}

const OCC_RE = /^([A-Z0-9.]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/

/** Parse an OCC option symbol ("XYZ261218P00042000"); null for anything else. */
export function parseOcc(symbol: string): OccSymbol | null {
  const m = OCC_RE.exec(symbol.replace(/\s+/g, ''))
  if (!m) return null
  const [, root, yy, mm, dd, cp, strike] = m
  return {
    root,
    expiry: `20${yy}-${mm}-${dd}`,
    type: cp === 'P' ? 'put' : 'call',
    strike: Number(strike) / 1000,
  }
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "18 Dec 26" from "2026-12-18". */
export function shortDate(iso: string): string {
  const [y, m, d] = iso.split('-').map(Number)
  if (!y || !m || !d) return iso
  return `${d} ${MONTHS[m - 1]} ${String(y).slice(2)}`
}

/** "XYZ 18 Dec 26 42 put" for an OCC symbol; the input unchanged otherwise. */
export function displaySymbol(symbol: string): string {
  const occ = parseOcc(symbol)
  if (!occ) return symbol
  return `${occ.root} ${shortDate(occ.expiry)} ${occ.strike.toLocaleString('en-US', { maximumFractionDigits: 3 })} ${occ.type}`
}
