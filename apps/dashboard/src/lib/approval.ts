import {
  APPROVAL_WINDOW_DAYS,
  RENDER_HOUR_ET,
  type ApprovalRequest,
  type IsoDate,
  type Look,
  type PublisherApproval,
} from '../contract.ts'
import { addDays, etToday, ZONES, zonedWallTimeToUtcMs } from './time.ts'

/**
 * What the home box reported about THIS request (same date and the same
 * requested_at instant), or null when its report is about another request
 * or it has not reported one.
 */
export function reportFor(
  request: ApprovalRequest | null | undefined,
  report: PublisherApproval | null | undefined,
): PublisherApproval | null {
  if (!request || !report || report.date !== request.date || report.requested_at == null) return null
  const a = Date.parse(report.requested_at)
  if (Number.isNaN(a) || a !== Date.parse(request.requested_at)) return null
  return report
}

/** Whether the look record already says what the request asks for. */
export function lookReflects(request: ApprovalRequest, look: Look): boolean {
  const signedOff = look.exists && look.approved === true
  return request.approved ? signedOff : !signedOff
}

/**
 * Whether a sign-off request is still waiting for the home box: it has not
 * finished with it (no report for it, or a failed apply it will retry), and
 * the look record does not reflect it yet.
 *
 * Once the home box reports the request `done` (applied, a no-op, or refused
 * for good), it is no longer pending even when the record disagrees: the
 * look may have been withdrawn, signed off or redrawn at home since, and the
 * publisher never handles that request again. The record is the truth then.
 */
export function isApprovalPending(
  request: ApprovalRequest | null | undefined,
  look: Look | null | undefined,
  report?: PublisherApproval | null,
): request is ApprovalRequest {
  if (!request || !look || request.date !== look.date) return false
  if (reportFor(request, report)?.done) return false
  return !lookReflects(request, look)
}

/** The sign-off state the operator has asked for: the request while pending, else the record. */
export function intendedApproval(
  request: ApprovalRequest | null | undefined,
  look: Look,
  report?: PublisherApproval | null,
): boolean {
  if (isApprovalPending(request, look, report)) return request.approved
  return look.exists && look.approved === true
}

/** The Worker accepts approvals for [ET today - 1, ET today + 3]. */
export function inApprovalWindow(date: string, nowMs: number): boolean {
  const today = etToday(nowMs)
  return date >= addDays(today, -APPROVAL_WINDOW_DAYS.before) && date <= addDays(today, APPROVAL_WINDOW_DAYS.after)
}

/** The UTC instant (ms) the 20:00 ET render of `date` starts. */
export function renderStartMs(date: IsoDate): number {
  return zonedWallTimeToUtcMs(date, RENDER_HOUR_ET, 0, ZONES.et.id)
}

/**
 * Whether a request sent now could still change the render of `date`: inside
 * the Worker's window and before that date's 20:00 ET render starts (the
 * home box refuses it from then on).
 */
export function canRequestApproval(date: IsoDate, nowMs: number): boolean {
  return inApprovalWindow(date, nowMs) && nowMs < renderStartMs(date)
}

/** The later of two requests by requested_at (either may be missing). */
export function newestRequest(
  a: ApprovalRequest | null | undefined,
  b: ApprovalRequest | null | undefined,
): ApprovalRequest | null {
  if (!a) return b ?? null
  if (!b) return a
  return Date.parse(b.requested_at) >= Date.parse(a.requested_at) ? b : a
}

/**
 * What the home box reported for THIS request, when that report means it
 * will not, or not yet, reach the look record:
 *   failed    rejected (malformed, or after the render), superseded by a
 *             later change at home, or an apply that kept failing
 *   retrying  the last apply failed and the home box will try again
 * Null when the home box has not handled this request or applied it fine.
 */
export function homeBoxProblem(
  request: ApprovalRequest | null | undefined,
  report: PublisherApproval | null | undefined,
): { state: 'failed' | 'retrying'; outcome: string } | null {
  const r = reportFor(request, report)
  if (!r || !/^(rejected|superseded|error)/.test(r.outcome)) return null
  return { state: r.done ? 'failed' : 'retrying', outcome: r.outcome }
}
