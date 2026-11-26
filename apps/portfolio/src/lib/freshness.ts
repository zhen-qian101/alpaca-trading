/**
 * When the published figures count as stale, so every page says so.
 *
 * A healthy pipeline never lets meta.as_of get much older than 7.5 hours:
 *   - the home box exports every 15 minutes and re-sends an unchanged file
 *     once an hour, so as_of keeps moving while the market is shut;
 *   - the deploy workflow runs every 30 minutes through the US session on
 *     weekdays and at most 6 hours apart at all other times, weekends and
 *     market holidays included (.github/workflows/portfolio.yml).
 * Nothing in the pipeline pauses for a weekend or a holiday, so neither can
 * trip this. 26 hours leaves room for GitHub to delay or drop three
 * scheduled runs in a row; past it, the export or the deploys have stopped.
 */
import { timestampToEpoch } from './format.ts'

export const STALE_AFTER_MS = 26 * 3_600_000

/** The longest gap between scheduled deploys, for the notice's wording. */
export const NORMAL_REFRESH_HOURS = 6

export function isStale(asOf: string, nowMs: number): boolean {
  const asOfMs = timestampToEpoch(asOf)
  return asOfMs !== null && nowMs - asOfMs > STALE_AFTER_MS
}
