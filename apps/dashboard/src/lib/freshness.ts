/**
 * How old is what the page is showing, and should it say so?
 *
 * The cloud only ever mirrors what the home box last pushed. A snapshot older
 * than FRESHNESS.staleAfterMs means the box stopped reporting (or the Worker
 * cannot be reached), and every number on screen is that old: the page must
 * say so loudly rather than look live.
 */
import { FRESHNESS, type IsoUtc } from '../contract.ts'
import { isoToEpoch } from './time.ts'

export type FreshnessState = 'fresh' | 'stale' | 'unknown'

/** Milliseconds since `asOf`; null when it cannot be parsed. Never negative. */
export function ageMs(asOf: IsoUtc | null | undefined, nowMs: number): number | null {
  const epoch = isoToEpoch(asOf)
  if (epoch === null) return null
  return Math.max(0, nowMs - epoch * 1000)
}

/** Stale strictly after the threshold: exactly 3 min old still counts as fresh. */
export function freshnessOf(
  asOf: IsoUtc | null | undefined,
  nowMs: number,
  staleAfterMs: number = FRESHNESS.staleAfterMs,
): FreshnessState {
  const age = ageMs(asOf, nowMs)
  if (age === null) return 'unknown'
  return age > staleAfterMs ? 'stale' : 'fresh'
}

/** Fraction of the freshness window still left (1 = just reported, 0 = stale). */
export function fuseRemaining(
  asOf: IsoUtc | null | undefined,
  nowMs: number,
  staleAfterMs: number = FRESHNESS.staleAfterMs,
): number {
  const age = ageMs(asOf, nowMs)
  if (age === null) return 0
  return Math.min(1, Math.max(0, 1 - age / staleAfterMs))
}

/**
 * How long, after the tab comes back, the page waits for the refetch that
 * the return triggers before it trusts an old snapshot as "the box went
 * quiet". The refetch normally answers in one round trip; this only bounds a
 * slow or retrying one.
 */
export const RESUME_CHECK_MS = 10_000

/** What the page knows about its own polling, for pageFreshness. */
export interface SnapshotPolling {
  /** Wall-clock ms of the Worker's last answer, data or final error (0 = none yet). */
  answeredAt: number
  /** The tab is hidden, so polling is paused. */
  hidden: boolean
  /** Wall-clock ms the tab last became visible again; null if it never was hidden. */
  resumedAt: number | null
}

/** Freshness as the page shows it: 'checking' is an old snapshot nobody has re-asked for yet. */
export type PageFreshness = FreshnessState | 'checking'

/**
 * freshnessOf, but an old snapshot only counts as 'stale' (the alert: "Home
 * box not reporting") when the page has actually been polling.
 *
 * Polling pauses while the tab is hidden; the clock does not. So after a few
 * minutes in the background the cached snapshot is older than 3 minutes,
 * and on return the alert fired at once, only to vanish a round trip later
 * when the refetch-on-focus answered: a screen reader announced a false
 * outage on every return. Until that refetch answers (or RESUME_CHECK_MS
 * passes), and while the tab is hidden, an old snapshot is 'checking'. A
 * page that has been polling all along, or a refetch that confirms the old
 * snapshot or fails, still reads 'stale'.
 */
export function pageFreshness(
  asOf: IsoUtc | null | undefined,
  nowMs: number,
  polling: SnapshotPolling,
  staleAfterMs: number = FRESHNESS.staleAfterMs,
): PageFreshness {
  const byAge = freshnessOf(asOf, nowMs, staleAfterMs)
  if (byAge !== 'stale') return byAge
  // Asked recently enough that a poll would not have run yet: the age is real.
  const unasked = (at: number) => at - polling.answeredAt > FRESHNESS.snapshotPollMs
  if (polling.hidden) return unasked(nowMs) ? 'checking' : 'stale'
  const { resumedAt } = polling
  if (resumedAt !== null && unasked(resumedAt) && nowMs - resumedAt < RESUME_CHECK_MS) return 'checking'
  return 'stale'
}
