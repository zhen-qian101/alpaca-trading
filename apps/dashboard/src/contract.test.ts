// The synthetic fixtures must conform to src/contract.ts. JSON imports cannot
// be checked against literal-union types at compile time, so the invariants
// that matter are checked here at runtime.
import { describe, expect, it } from 'vitest'

import {
  chartKey,
  ISO_DATE_RE,
  isBuilderError,
  R2_KEYS,
  SNAPSHOT_SCHEMA,
  SYMBOL_RE,
  type Board,
  type Executions,
  type Journal,
  type OptionsSleeve,
  type Portfolio,
  type SymbolChart,
} from './contract.ts'
import {
  approvalFixture,
  chartFixtures,
  historyFixture,
  snapshotFixture,
} from './test/fixtures.ts'

const ISO_UTC_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/

function descending(values: number[]): boolean {
  return values.every((v, i) => i === 0 || values[i - 1] >= v)
}

describe('contract helpers', () => {
  it('maps a symbol to its R2 key', () => {
    expect(chartKey('BTC/USD')).toBe('BTC-USD')
    expect(chartKey('AAPL')).toBe('AAPL')
    expect(R2_KEYS.chart('QBIT/USD')).toBe('chart/QBIT-USD.json')
    expect(R2_KEYS.lookAsset('2026-03-04', 'plate')).toBe('look/2026-03-04/plate.png')
    expect(R2_KEYS.approval('2026-03-04')).toBe('approvals/2026-03-04.json')
  })

  it('accepts trading symbols and rejects anything path-like', () => {
    for (const ok of ['AAPL', 'BRK.B', 'BTC/USD', 'QBIT/USD', 'SOXL']) {
      expect(SYMBOL_RE.test(ok), ok).toBe(true)
    }
    for (const bad of ['', 'aapl', '../x', 'A/B/C', 'BTC/', '/USD', 'A B', 'AAPL\n', 'A%2FB']) {
      expect(SYMBOL_RE.test(bad), JSON.stringify(bad)).toBe(false)
    }
  })

  it('accepts only plain dates', () => {
    expect(ISO_DATE_RE.test('2026-03-04')).toBe(true)
    for (const bad of ['2026-3-4', '2026-03-04/..', '../2026-03-04', '2026-03-04 ']) {
      expect(ISO_DATE_RE.test(bad), bad).toBe(false)
    }
  })

  it('recognises builder errors', () => {
    expect(isBuilderError({ error: 'broker unreachable' })).toBe(true)
    expect(isBuilderError({ equity: 1 })).toBe(false)
    expect(isBuilderError(null)).toBe(false)
  })
})

describe('snapshot fixture', () => {
  const s = snapshotFixture

  it('has the envelope', () => {
    expect(s.schema).toBe(SNAPSHOT_SCHEMA)
    expect(s.as_of).toMatch(ISO_UTC_RE)
    expect(typeof s.publisher.version).toBe('string')
    expect(typeof s.publisher.host).toBe('string')
    expect(s.symbols.length).toBeGreaterThan(0)
    for (const sym of s.symbols) expect(SYMBOL_RE.test(sym), sym).toBe(true)
    expect(s.symbols.some((sym) => sym.includes('/'))).toBe(true)
  })

  it('portfolio is internally consistent', () => {
    expect(isBuilderError(s.portfolio)).toBe(false)
    const p = s.portfolio as Portfolio
    expect(p.day_pl).toBeCloseTo(p.equity - p.last_equity, 2)
    expect(p.day_pl_pct).toBeCloseTo(p.equity / p.last_equity - 1, 8)
    expect(descending(p.positions.map((x) => x.market_value))).toBe(true)
    for (const pos of p.positions) {
      expect(['long', 'short', null]).toContain(pos.side)
      if (pos.stop_price !== null && pos.stop_distance_pct !== null) {
        expect(pos.stop_distance_pct).toBeCloseTo(pos.current_price / pos.stop_price - 1, 8)
      }
    }
    expect(p.halt.used_frac).toBeGreaterThanOrEqual(0)
    expect(p.halt.used_frac).toBeLessThanOrEqual(1)
  })

  it('board rows are ranked and contingencies well formed', () => {
    const b = s.board as Board
    expect(isBuilderError(b)).toBe(false)
    expect(descending(b.rows.map((r) => r.priority))).toBe(true)
    for (const r of b.rows) {
      expect(['long', 'short', 'flat']).toContain(r.bias)
      for (const c of r.contingencies) {
        expect(['close_above', 'close_below']).toContain(c.condition)
        expect(c.action).toBe('long')
        expect(c.valid_until).toMatch(ISO_UTC_RE)
      }
    }
    expect(b.rows.some((r) => r.contingencies.length > 0)).toBe(true)
  })

  it('journal and executions are newest first', () => {
    expect(isBuilderError(s.journal)).toBe(false)
    const journal = s.journal as Journal
    const ts = journal.rows.map((r) => r._ts ?? 0)
    expect(descending(ts)).toBe(true)
    for (const r of journal.rows) expect(typeof r.event).toBe('string')
    const ex = s.executions as Executions
    expect(descending(ex.rows.map((r) => r.ts ?? 0))).toBe(true)
    for (const r of ex.rows) expect(['buy', 'sell']).toContain(r.side)
    const realized = ex.rows.reduce((sum, r) => sum + (r.pnl ?? 0), 0)
    expect(ex.realized_pnl).toBeCloseTo(realized, 2)
  })

  it('look points at the Worker asset route for its own date', () => {
    const look = s.look
    expect(isBuilderError(look)).toBe(false)
    if (isBuilderError(look) || !look.exists) throw new Error('fixture look should exist')
    expect(look.date).toMatch(ISO_DATE_RE)
    // Versioned by content (the first 12 hex of the image's SHA-256), so a
    // same-date redraw is a new URL.
    expect(look.plate_url).toMatch(new RegExp(`^/api/look/asset\\?date=${look.date}&kind=plate&v=[0-9a-f]{12}$`))
    expect(look.keyframe_url).toMatch(new RegExp(`^/api/look/asset\\?date=${look.date}&kind=keyframe&v=[0-9a-f]{12}$`))
    expect(look.renders_with).toBe(look.approved ? 'look' : 'locked')
  })

  it('options sleeve is consistent with its OCC symbol', () => {
    const o = s.options as OptionsSleeve
    expect(isBuilderError(o)).toBe(false)
    const put = o.short_put!
    expect(put.symbol.startsWith(o.underlying)).toBe(true)
    expect(put.strike).toBe(Number(put.symbol.slice(-8)) / 1000)
    expect(put.symbol.at(-9)).toBe('P')
    expect(put.contracts).toBeGreaterThan(0)
    expect(put.market_value).toBeLessThan(0)
    expect(put.strike_distance_pct).toBeCloseTo(put.underlying_price! / put.strike! - 1, 8)
    expect(s.publisher.approval ?? null).toBeNull()
  })

  it('approval request matches the look date', () => {
    expect(approvalFixture.date).toMatch(ISO_DATE_RE)
    expect(approvalFixture.requested_at).toMatch(ISO_UTC_RE)
    expect(typeof approvalFixture.approved).toBe('boolean')
  })
})

describe('chart fixtures', () => {
  const entries = Object.entries(chartFixtures)

  it('cover a crypto symbol with a slash', () => {
    expect(entries.length).toBeGreaterThanOrEqual(2)
    expect(entries.some(([, c]) => Object.keys(c.symbols).some((s) => s.includes('/')))).toBe(true)
  })

  it.each(entries)('%s is keyed, true-UTC and aligned', (key, chart) => {
    expect(chart.schema).toBe(SNAPSHOT_SCHEMA)
    expect(chart.time_basis).toBe('utc')
    expect(chart.as_of).toMatch(ISO_UTC_RE)
    const symbols = Object.keys(chart.symbols)
    expect(symbols).toHaveLength(1)
    expect(chartKey(symbols[0])).toBe(key)
    const c: SymbolChart = chart.symbols[symbols[0]]

    const times = c.candles.map((k) => k.time)
    expect(times.every((t) => Number.isInteger(t))).toBe(true)
    expect(times.every((t, i) => i === 0 || t > times[i - 1])).toBe(true)
    // Seconds, not milliseconds.
    expect(times[0]).toBeLessThan(10_000_000_000)

    const known = new Set(times)
    for (const series of [c.bbUpper, c.bbBasis, c.bbLower, c.rsi, c.macdHist]) {
      for (const p of series) expect(known.has(p.time)).toBe(true)
    }
    for (const m of c.markers) expect(known.has(m.time)).toBe(true)
    expect(descending(c.markers.map((m) => -m.time))).toBe(true)
    for (const k of c.candles) {
      expect(k.high).toBeGreaterThanOrEqual(Math.max(k.open, k.close))
      expect(k.low).toBeLessThanOrEqual(Math.min(k.open, k.close))
    }
    expect(c.bbLabel === '').toBe(c.bbUpper.length === 0)
  })

  it('equity fixtures sit in US trading hours when read as UTC (no Melbourne shift)', () => {
    const hour = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/New_York',
      hour: 'numeric',
      hourCycle: 'h23',
    })
    for (const [key, chart] of entries) {
      if (key.includes('-')) continue // crypto trades around the clock
      for (const k of Object.values(chart.symbols)[0].candles) {
        const h = Number(hour.format(new Date(k.time * 1000)))
        expect(h, `${key} @ ${k.time}`).toBeGreaterThanOrEqual(9)
        expect(h, `${key} @ ${k.time}`).toBeLessThanOrEqual(15)
      }
    }
  })
})

describe('history fixture', () => {
  const h = historyFixture

  it('only carries /media URLs inside each edition', () => {
    expect(JSON.stringify(h)).not.toContain('/archive/')
    for (const ed of h.briefings) {
      const base = `/media/${ed.date}/${ed.slot}/`
      const urls = [ed.briefing, ed.briefing_zh, ed.cover, ed.cover_zh, ed.cover_zh_bili]
        .concat(ed.clips.flatMap((c) => [c.file, c.file_zh]))
        .filter((u): u is NonNullable<typeof u> => u !== undefined)
      for (const url of urls) {
        expect(url.startsWith(base), url).toBe(true)
        // every media URL carries its content version (web/publisher.py media_url)
        expect(url, url).toMatch(/^\/media\/[^?]+\?v=[0-9a-f]{12}$/)
      }
      expect(ed.briefing.split('?')[0]).toBe(base + ed.briefing_file)
      expect(ed.kind).toBe(ed.slot.startsWith('slideshow') ? 'slideshow' : 'video')
      expect(ed.local).toBe(false)
      for (const c of ed.clips) {
        // per-scene clips are not uploaded; a surviving file is a chapter of the full video
        if (c.file !== undefined) {
          expect(c.file).toBe(ed.briefing)
          expect(typeof c.start_s).toBe('number')
        }
      }
    }
  })

  it('is newest first', () => {
    const keys = h.briefings.map((b) => `${b.date} ${b.generated_at}`)
    expect([...keys].sort().reverse()).toEqual(keys)
  })
})
