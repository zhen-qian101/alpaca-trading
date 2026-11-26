import { createContext, useContext } from 'react'

import type { PageFreshness } from './freshness.ts'

/**
 * The page-wide verdict on the snapshot, computed once by the shell and
 * changing only when the verdict flips (not every second), so panels can
 * restyle themselves as stale without re-rendering on every clock tick.
 */
export interface SnapshotStatus {
  /**
   * Age-based: older than FRESHNESS.staleAfterMs is "stale", except while
   * the page has not re-asked since its tab was hidden ("checking", shown
   * quietly; see pageFreshness).
   */
  freshness: PageFreshness
  /** The latest poll failed (the numbers on screen are from an earlier one). */
  failing: boolean
  /** Access session gone. */
  signedOut: boolean
}

export const SnapshotStatusContext = createContext<SnapshotStatus>({
  freshness: 'unknown',
  failing: false,
  signedOut: false,
})

export function useSnapshotStatus(): SnapshotStatus {
  return useContext(SnapshotStatusContext)
}

/** True when panels showing snapshot data should mark themselves stale. */
export function isStaleStatus(status: SnapshotStatus): boolean {
  return status.freshness === 'stale' || status.failing || status.signedOut
}
