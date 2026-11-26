import { describe, expect, it } from 'vitest'

import { displaySymbol, money, parseOcc, pct, signedMoney, signedPct } from './format.ts'

describe('number formatting', () => {
  it('always prints the sign of a change, with a real minus', () => {
    expect(signedMoney(1685.38)).toBe('+$1,685.38')
    expect(signedMoney(-554.4)).toBe('−$554.40')
    expect(signedMoney(0)).toBe('$0.00')
    expect(signedPct(0.016774)).toBe('+1.68%')
    expect(signedPct(-0.0559)).toBe('−5.59%')
  })

  it('formats money and fractions', () => {
    expect(money(102161.29)).toBe('$102,161.29')
    expect(money(-62)).toBe('−$62.00')
    expect(pct(0.05, 0)).toBe('5%')
    expect(money(null)).toBe('—')
  })
})

describe('OCC option symbols', () => {
  it('parses a put', () => {
    expect(parseOcc('ORBT260320P00040000')).toEqual({ root: 'ORBT', expiry: '2026-03-20', type: 'put', strike: 40 })
    expect(parseOcc('ACME')).toBeNull()
    expect(parseOcc('QBIT/USD')).toBeNull()
  })

  it('displays options readably and leaves other symbols alone', () => {
    expect(displaySymbol('ORBT260320P00040000')).toBe('ORBT 20 Mar 26 40 put')
    expect(displaySymbol('NOVA261218C00057500')).toBe('NOVA 18 Dec 26 57.5 call')
    expect(displaySymbol('QBIT/USD')).toBe('QBIT/USD')
  })
})
