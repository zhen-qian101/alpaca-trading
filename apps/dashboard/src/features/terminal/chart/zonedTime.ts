/**
 * Where the chart's date labels go, in the chosen zone.
 *
 * lightweight-charts decides which bars carry a Day, Month or Year label
 * (its "tick weights") with the UTC getters, so with TRUE UTC times its
 * "24 Feb" label sits on the bar at 00:00 UTC, which is 19:00 ET the day
 * before (11:00 in Melbourne), and the real midnight bar is labelled
 * "00:00". Formatting the labels in the zone cannot fix that: the formatter
 * is only asked about the bars the library already picked.
 *
 * So the chart is built with the library's own time scale behaviour, with
 * one method replaced: the weights are computed from the zone's wall clock.
 * The data stays true UTC (shifting the times instead would collide in the
 * repeated hour when DST ends, which the publisher avoids for the same
 * reason).
 */
import { defaultHorzScaleBehavior, type IHorzScaleBehavior, type Time } from 'lightweight-charts'

import { wallClockMs } from '../../../lib/time.ts'

/** lightweight-charts' TickMarkWeight values (a const enum it does not export). */
export const TICK_WEIGHT = {
  LessThanSecond: 0,
  Second: 10,
  Minute1: 20,
  Minute5: 21,
  Minute30: 22,
  Hour1: 30,
  Hour3: 31,
  Hour6: 32,
  Hour12: 33,
  Day: 50,
  Month: 60,
  Year: 70,
} as const

const HOUR = 3_600_000
/** Coarsest first, as the library checks them. */
const INTRADAY: readonly [divisorMs: number, weight: number][] = [
  [12 * HOUR, TICK_WEIGHT.Hour12],
  [6 * HOUR, TICK_WEIGHT.Hour6],
  [3 * HOUR, TICK_WEIGHT.Hour3],
  [HOUR, TICK_WEIGHT.Hour1],
  [30 * 60_000, TICK_WEIGHT.Minute30],
  [5 * 60_000, TICK_WEIGHT.Minute5],
  [60_000, TICK_WEIGHT.Minute1],
  [1_000, TICK_WEIGHT.Second],
]

/** The library's weightByTime, on two wall-clock values (see wallClockMs). */
function weightBetween(current: number, previous: number): number {
  const c = new Date(current)
  const p = new Date(previous)
  if (c.getUTCFullYear() !== p.getUTCFullYear()) return TICK_WEIGHT.Year
  if (c.getUTCMonth() !== p.getUTCMonth()) return TICK_WEIGHT.Month
  if (c.getUTCDate() !== p.getUTCDate()) return TICK_WEIGHT.Day
  for (const [divisor, weight] of INTRADAY) {
    if (Math.floor(previous / divisor) !== Math.floor(current / divisor)) return weight
  }
  return TICK_WEIGHT.LessThanSecond
}

/** The slice of a time scale point this needs: its weight (written) and time. */
export interface WeightedPoint {
  timeWeight: number
}

/**
 * The library's fillWeightsForPoints, in `zoneId`: point i's weight says what
 * changed on the zone's wall clock since point i - 1, and the first point's
 * weight is guessed from the average spacing, exactly as the library does.
 * `secondsOf` gives a point's TRUE UTC time in epoch seconds.
 */
export function fillZonedWeights<P extends WeightedPoint>(
  points: readonly P[],
  startIndex: number,
  zoneId: string,
  secondsOf: (point: P) => number,
): void {
  if (points.length === 0) return
  const wall = (seconds: number) => wallClockMs(seconds * 1000, zoneId)
  let prevTime = startIndex === 0 ? null : secondsOf(points[startIndex - 1])
  let prevWall = prevTime === null ? null : wall(prevTime)
  let totalTimeDiff = 0
  for (let i = startIndex; i < points.length; i++) {
    const time = secondsOf(points[i])
    const currentWall = wall(time)
    if (prevWall !== null) points[i].timeWeight = weightBetween(currentWall, prevWall)
    totalTimeDiff += time - (prevTime ?? time)
    prevTime = time
    prevWall = currentWall
  }
  if (startIndex === 0 && points.length > 1) {
    const averageTimeDiff = Math.ceil(totalTimeDiff / (points.length - 1))
    const first = secondsOf(points[0])
    points[0].timeWeight = weightBetween(wall(first), wall(first - averageTimeDiff))
  }
}

type TimePoints = Parameters<IHorzScaleBehavior<Time>['fillWeightsForPoints']>[0]

/**
 * A time scale behaviour for createChartEx that places Day / Month / Year
 * ticks at midnight in `zoneId`. A chart keeps the behaviour it was built
 * with, so a zone change needs a new chart.
 */
export function zonedTimeScale(zoneId: string): IHorzScaleBehavior<Time> {
  const Base = defaultHorzScaleBehavior()
  class ZonedTimeScale extends Base {
    override fillWeightsForPoints(points: TimePoints, startIndex: number): void {
      fillZonedWeights(points as unknown as WeightedPoint[], startIndex, zoneId, (p) =>
        Number(this.key((p as unknown as TimePoints[number]).time)),
      )
    }
  }
  return new ZonedTimeScale()
}
