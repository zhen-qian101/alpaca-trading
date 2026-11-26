import { describe, expect, it } from 'vitest'

import fixture from '../../fixtures/portfolio.json'
import { parsePortfolio } from './portfolio.ts'

const clone = (): Record<string, unknown> => structuredClone(fixture) as unknown as Record<string, unknown>

// Walks a dotted path ("account.daily.0") and returns the parent and key.
function at(obj: Record<string, unknown>, path: string): [Record<string, unknown>, string] {
  const parts = path.split('.')
  const last = parts.pop()!
  let cur: unknown = obj
  for (const p of parts) cur = (cur as Record<string, unknown>)[p]
  return [cur as Record<string, unknown>, last]
}

function withChange(path: string, change: (parent: Record<string, unknown>, key: string) => void) {
  const doc = clone()
  const [parent, key] = at(doc, path)
  change(parent, key)
  return parsePortfolio(doc)
}

describe('parsePortfolio', () => {
  it('accepts the synthetic fixture', () => {
    const result = parsePortfolio(fixture)
    expect(result.ok).toBe(true)
  })

  it('accepts a file where every source failed', () => {
    const doc = clone()
    Object.assign(doc, {
      account: null,
      positions: { equities: null, options: null },
      trades: null,
      signals: null,
      strategy: null,
      recaps: null,
    })
    ;(doc.meta as Record<string, unknown>).unavailable = ['account', 'positions', 'options', 'trades', 'signals', 'strategy', 'recaps']
    expect(parsePortfolio(doc).ok).toBe(true)
  })

  it.each([
    ['the root', ''],
    ['meta', 'meta'],
    ['the account', 'account'],
    ['a daily point', 'account.daily.0'],
    ['a position', 'positions.equities.0'],
    ['an option', 'positions.options.0'],
    ['trade stats', 'trades.stats'],
    ['a round trip', 'trades.round_trips.0'],
    ['a signal row', 'signals.rows.0'],
    ['a contingency', 'signals.rows.5.contingencies.0'],
    ['a strategy symbol', 'strategy.symbols.0'],
    ['a strategy spec', 'strategy.symbols.0.strategies.0'],
    ['the risk block', 'strategy.risk'],
    ['a recap', 'recaps.0'],
  ])('refuses an extra field on %s', (_name, path) => {
    const doc = clone()
    const target = path ? (at(doc, `${path}.x`)[0] as Record<string, unknown>) : doc
    target.notes = 'a field nobody whitelisted'
    const result = parsePortfolio(doc)
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.errors.join('\n')).toContain('a field the schema does not allow')
      // Errors name the place, never the value or the key.
      expect(result.errors.join('\n')).not.toContain('nobody whitelisted')
      expect(result.errors.join('\n')).not.toContain('notes')
    }
  })

  it('refuses an intraday equity series: the account is daily only', () => {
    for (const intraday of [
      { date: '2026-06-12', points: [{ t: '2026-06-12T13:30:00Z', equity: 1 }] },
      { date: '2026-06-12', points: [] },
      null,
    ]) {
      const result = withChange('account', (p, k) => {
        ;(p[k] as Record<string, unknown>).intraday = intraday
      })
      expect(result.ok).toBe(false)
    }
  })

  it('refuses the risk fields nothing enforces, and an unknown extended-hours permission', () => {
    for (const [key, value] of [
      ['max_open_positions', 1],
      ['max_open_positions_hard_cap', 10],
      ['allow_extended_hours', null],
    ] as const) {
      const result = withChange('strategy.risk', (p, k) => {
        ;(p[k] as Record<string, unknown>)[key] = value
      })
      expect(result.ok, key).toBe(false)
    }
  })

  it('refuses a parameter that belongs to another strategy type', () => {
    const result = withChange('strategy.symbols.0.strategies.0', (p, k) => {
      ;(p[k] as Record<string, unknown>).entry_window = 55
    })
    expect(result.ok).toBe(false)
  })

  it('refuses an unknown strategy type', () => {
    const result = withChange('strategy.symbols.0.strategies.0', (p, k) => {
      ;(p[k] as Record<string, unknown>).type = 'secret_sauce'
    })
    expect(result.ok).toBe(false)
  })

  it.each([
    ['a missing required field', 'account.equity', (p: Record<string, unknown>, k: string) => delete p[k]],
    ['a string where a number belongs', 'account.equity', (p: Record<string, unknown>, k: string) => (p[k] = '1')],
    ['NaN', 'account.equity', (p: Record<string, unknown>, k: string) => (p[k] = Number.NaN)],
    ['Infinity', 'trades.stats.realized_pnl', (p: Record<string, unknown>, k: string) => (p[k] = Infinity)],
    ['a fraction above 1', 'trades.stats.win_rate', (p: Record<string, unknown>, k: string) => (p[k] = 1.5)],
    ['a positive drawdown', 'account.max_drawdown_pct', (p: Record<string, unknown>, k: string) => (p[k] = 0.1)],
    ['a non-integer count', 'trades.stats.wins', (p: Record<string, unknown>, k: string) => (p[k] = 1.5)],
    ['a lowercase symbol', 'trades.round_trips.0.symbol', (p: Record<string, unknown>, k: string) => (p[k] = 'acme')],
    ['a short side on a round trip', 'trades.round_trips.0.side', (p: Record<string, unknown>, k: string) => (p[k] = 'short')],
    ['a timestamp with an offset', 'meta.as_of', (p: Record<string, unknown>, k: string) => (p[k] = '2026-06-12T22:05:00+00:00')],
    ['a bad YouTube id', 'recaps.0.youtube_id', (p: Record<string, unknown>, k: string) => (p[k] = 'not-an-id')],
    ['a title on a recap (a language model writes them)', 'recaps.0.title', (p: Record<string, unknown>, k: string) => (p[k] = 'Markets rally')],
    ['an unknown bias', 'signals.rows.0.bias', (p: Record<string, unknown>, k: string) => (p[k] = 'maybe')],
    ['a schema version other than 1', 'meta.schema', (p: Record<string, unknown>, k: string) => (p[k] = 2)],
    ['an unknown unavailable section', 'meta.unavailable', (p: Record<string, unknown>, k: string) => (p[k] = ['logs'])],
    ['a repeated unavailable section', 'meta.unavailable', (p: Record<string, unknown>, k: string) => (p[k] = ['trades', 'trades'])],
    ['an array where an object belongs', 'positions', (p: Record<string, unknown>, k: string) => (p[k] = [])],
  ])('refuses %s', (_name, path, change) => {
    expect(withChange(path, change).ok).toBe(false)
  })

  it('refuses too many recaps', () => {
    const doc = clone()
    const recaps = doc.recaps as unknown[]
    doc.recaps = Array.from({ length: 13 }, () => recaps[0])
    expect(parsePortfolio(doc).ok).toBe(false)
  })

  it('refuses things that are not a portfolio at all', () => {
    for (const value of [null, [], 'portfolio', 42, { meta: {} }]) expect(parsePortfolio(value).ok).toBe(false)
  })
})
