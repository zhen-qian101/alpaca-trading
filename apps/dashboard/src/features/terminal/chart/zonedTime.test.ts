import { defaultHorzScaleBehavior, type IHorzScaleBehavior, type Time, type UTCTimestamp } from 'lightweight-charts'
import { describe, expect, it } from 'vitest'

import { chartFixtures } from '../../../test/fixtures.ts'
import { dateInZone, formatClock, ZONES } from '../../../lib/time.ts'
import { tickLabel, TICK } from './chartMath.ts'
import { fillZonedWeights, TICK_WEIGHT, zonedTimeScale } from './zonedTime.ts'

type TimePoints = Parameters<IHorzScaleBehavior<Time>['fillWeightsForPoints']>[0]

/** Time scale points as the library builds them, `time` in its own internal form. */
const toInternal = new (defaultHorzScaleBehavior())()
const pointsAt = (times: readonly number[]) =>
  times.map((t) => ({
    time: toInternal.convertHorzItemToInternal(t as UTCTimestamp),
    timeWeight: 0,
    originalTime: t,
  })) as unknown as TimePoints
const weightsOf = (points: TimePoints) => points.map((p) => Number(p.timeWeight))
const qbitTimes = chartFixtures['QBIT-USD'].symbols['QBIT/USD'].candles.map((c) => c.time)
const hourly = (fromIso: string, n: number) => Array.from({ length: n }, (_, i) => Date.parse(fromIso) / 1000 + i * 3600)

/** The bars that carry a date label (Day, Month or Year tick). */
function dateTicks(times: readonly number[], zoneId: string): number[] {
  const points = pointsAt(times)
  zonedTimeScale(zoneId).fillWeightsForPoints(points, 0)
  return times.filter((_, i) => Number(points[i].timeWeight) >= TICK_WEIGHT.Day)
}

describe('date labels in the chosen zone', () => {
  it('is the library itself when the zone is UTC', () => {
    // Same algorithm, first-point guess included: only the clock differs.
    const library = pointsAt(qbitTimes)
    new (defaultHorzScaleBehavior())().fillWeightsForPoints(library, 0)
    const ours = pointsAt(qbitTimes)
    zonedTimeScale('UTC').fillWeightsForPoints(ours, 0)
    expect(weightsOf(ours)).toEqual(weightsOf(library))

    // ... and when it refills from a later index, as it does on new bars.
    new (defaultHorzScaleBehavior())().fillWeightsForPoints(library, 150)
    zonedTimeScale('UTC').fillWeightsForPoints(ours, 150)
    expect(weightsOf(ours)).toEqual(weightsOf(library))
  })

  it('puts every date label on the bar at midnight ET, not at 00:00 UTC', () => {
    const ticks = dateTicks(qbitTimes, ZONES.et.id)
    expect(ticks.length).toBeGreaterThanOrEqual(7)
    for (const t of ticks) expect(formatClock(t, 'et')).toBe('00:00')
    // The library's own choice lands at 19:00 ET (00:00 UTC) the evening before.
    expect(ticks).not.toContain(Date.parse('2026-02-25T00:00:00Z') / 1000)
    expect(ticks).toContain(Date.parse('2026-02-25T05:00:00Z') / 1000)
    // Exactly one per ET day after the first bar.
    const days = new Set(qbitTimes.map((t) => dateInZone(t * 1000, ZONES.et.id)))
    expect(ticks.length).toBe(days.size - 1)
  })

  it('labels the midnight bar with its date in that zone', () => {
    const points = pointsAt(qbitTimes)
    zonedTimeScale(ZONES.et.id).fillWeightsForPoints(points, 0)
    const i = qbitTimes.indexOf(Date.parse('2026-02-25T05:00:00Z') / 1000)
    expect(Number(points[i].timeWeight)).toBe(TICK_WEIGHT.Day)
    expect(tickLabel(qbitTimes[i], TICK.DayOfMonth, 'et')).toBe('25 Feb')
    // The bar before it (23:00 ET) is an ordinary hour tick.
    expect(Number(points[i - 1].timeWeight)).toBeLessThan(TICK_WEIGHT.Day)
  })

  it('follows Melbourne midnight in Melbourne mode', () => {
    for (const t of dateTicks(qbitTimes, ZONES.melbourne.id)) expect(formatClock(t, 'melbourne')).toBe('00:00')
  })

  it('marks a new month and a new year on the zone clock', () => {
    const times = hourly('2026-12-31T00:00:00Z', 48) // across ET New Year (05:00Z on Jan 1)
    const points = pointsAt(times)
    zonedTimeScale(ZONES.et.id).fillWeightsForPoints(points, 0)
    const at = (iso: string) => Number(points[times.indexOf(Date.parse(iso) / 1000)].timeWeight)
    expect(at('2027-01-01T05:00:00Z')).toBe(TICK_WEIGHT.Year)
    expect(at('2027-01-01T00:00:00Z')).toBeLessThan(TICK_WEIGHT.Day)
    const feb = hourly('2026-02-28T00:00:00Z', 30)
    const fp = pointsAt(feb)
    zonedTimeScale(ZONES.et.id).fillWeightsForPoints(fp, 0)
    expect(Number(fp[feb.indexOf(Date.parse('2026-03-01T05:00:00Z') / 1000)].timeWeight)).toBe(TICK_WEIGHT.Month)
  })

  it('keeps one label per day across the end of DST', () => {
    // US clocks fall back at 06:00Z on 2026-11-01: 01:00 ET happens twice.
    const times = hourly('2026-10-31T12:00:00Z', 48)
    const ticks = dateTicks(times, ZONES.et.id)
    expect(ticks).toEqual([Date.parse('2026-11-01T04:00:00Z') / 1000, Date.parse('2026-11-02T05:00:00Z') / 1000])
  })

  it('needs no points to do nothing', () => {
    expect(() => fillZonedWeights([], 0, ZONES.et.id, () => 0)).not.toThrow()
  })
})
