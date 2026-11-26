import { describe, expect, it } from 'vitest'

import {
  clock,
  etDate,
  etDateTime,
  etIsoDate,
  etOffsetSeconds,
  holding,
  longDate,
  MINUS,
  money,
  pct,
  pctShort,
  price,
  quantity,
  shortDate,
  signedMoney,
  signedPct,
  signedPctShort,
  spokenDuration,
  tone,
} from './format.ts'

describe('money', () => {
  it('formats dollars and cents', () => {
    expect(money(103827.51)).toBe('$103,827.51')
    expect(money(100000, { whole: true })).toBe('$100,000')
  })

  it('uses a true minus and never shows a negative zero', () => {
    expect(money(-135.25)).toBe(`${MINUS}$135.25`)
    expect(money(-0.001)).toBe('$0.00')
  })

  it('signs gains and losses', () => {
    expect(signedMoney(374.63)).toBe('+$374.63')
    expect(signedMoney(-164.23)).toBe(`${MINUS}$164.23`)
    expect(signedMoney(0)).toBe('$0.00')
  })

  it('shows a dash for a missing figure', () => {
    expect(money(null)).toBe('–')
    expect(signedMoney(undefined)).toBe('–')
  })
})

describe('percentages (fractions in, percent out)', () => {
  it('formats fractions', () => {
    expect(pct(0.5, 0)).toBe('50%')
    expect(pct(-0.082306)).toBe(`${MINUS}8.23%`)
    expect(signedPct(0.038275)).toBe('+3.83%')
    expect(signedPct(-0.019763)).toBe(`${MINUS}1.98%`)
    expect(signedPct(0.00001)).toBe('0.00%')
  })

  it('labels quarter-percent grid points exactly, signed, with no trailing zeros', () => {
    const edges = [-3, -2, -1, 0, 1, 2, 3].map((i) => i * 0.0025)
    expect(edges.map(signedPctShort)).toEqual([
      `${MINUS}0.75%`,
      `${MINUS}0.5%`,
      `${MINUS}0.25%`,
      '0%',
      '+0.25%',
      '+0.5%',
      '+0.75%',
    ])
    // symmetric around zero, unlike rounding to a tenth (−0.7%, −0.5%, −0.2% / +0.3%, +0.5%, +0.8%)
    expect(signedPctShort(-0.0025).slice(1)).toBe(signedPctShort(0.0025).slice(1))
    expect(signedPctShort(0.05)).toBe('+5%')
    expect(signedPctShort(-0.025)).toBe(`${MINUS}2.5%`)
    expect(signedPctShort(-0.00001)).toBe('0%')
    expect(signedPctShort(null)).toBe('–')
  })

  it('trims trailing zeros for settings', () => {
    expect(pctShort(0.02)).toBe('2%')
    expect(pctShort(0.025)).toBe('2.5%')
    expect(pctShort(0.97)).toBe('97%')
  })
})

describe('prices and quantities', () => {
  it('keeps cents, and more places under a dollar', () => {
    expect(price(61234.5)).toBe('61,234.50')
    expect(price(0.5123)).toBe('0.5123')
  })

  it('shows fractional crypto quantities', () => {
    expect(quantity(0.5)).toBe('0.5')
    expect(quantity(120)).toBe('120')
    expect(quantity(0.123456789)).toBe('0.123457')
  })
})

describe('tone', () => {
  it('follows the sign', () => {
    expect(tone(1)).toBe('gain')
    expect(tone(-1)).toBe('loss')
    expect(tone(0)).toBe('even')
    expect(tone(null)).toBe('even')
  })
})

describe('dates and times', () => {
  it('shows session dates as written, whatever the viewer zone', () => {
    expect(longDate('2026-06-12')).toBe('Jun 12, 2026')
    expect(shortDate('2026-01-05')).toBe('Jan 5')
    expect(longDate('2026-02-31')).toBe('2026-02-31')
  })

  it('shows UTC timestamps in New York time, across daylight saving', () => {
    expect(etDateTime('2026-06-12T22:05:00Z')).toBe('Jun 12, 2026, 6:05 PM ET')
    expect(etDateTime('2026-01-06T15:30:00Z')).toBe('Jan 6, 2026, 10:30 AM ET')
    expect(etDate('2026-06-13T02:00:00Z')).toBe('Jun 12, 2026')
    expect(etIsoDate(Date.parse('2026-06-13T02:00:00Z'))).toBe('2026-06-12')
  })

  it('knows the New York offset', () => {
    expect(etOffsetSeconds(Date.parse('2026-06-12T13:30:00Z'))).toBe(-4 * 3600)
    expect(etOffsetSeconds(Date.parse('2026-01-12T14:30:00Z'))).toBe(-5 * 3600)
  })

  it('formats holding periods and video lengths', () => {
    expect(holding('2026-05-19T14:30:00Z', '2026-05-19T18:45:00Z')).toBe('4h 15m')
    expect(holding('2026-05-19T14:30:00Z', '2026-05-19T15:00:00Z')).toBe('30m')
    expect(holding('2026-05-19T14:30:00Z', '2026-05-22T16:30:00Z')).toBe('3d 2h')
    expect(clock(65)).toBe('1:05')
    expect(clock(3725)).toBe('1:02:05')
    expect(spokenDuration(65)).toBe('1 minute 5 seconds')
  })
})
