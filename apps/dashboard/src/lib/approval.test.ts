import { describe, expect, it } from 'vitest'

import type { ApprovalRequest, LookPresent, PublisherApproval } from '../contract.ts'
import { approvalFixture, snapshotFixture } from '../test/fixtures.ts'
import {
  canRequestApproval,
  homeBoxProblem,
  inApprovalWindow,
  intendedApproval,
  isApprovalPending,
  newestRequest,
  renderStartMs,
} from './approval.ts'

const look = snapshotFixture.look as LookPresent
const withApproved = (approved: boolean | null): LookPresent => ({ ...look, approved })
const req = (approved: boolean): ApprovalRequest => ({ ...approvalFixture, date: look.date, approved })

describe('approval requests', () => {
  it('a sign-off stays pending until the look reads approved', () => {
    expect(isApprovalPending(req(true), withApproved(null))).toBe(true)
    expect(isApprovalPending(req(true), withApproved(true))).toBe(false)
  })

  it('a withdrawal stays pending while the look still reads approved', () => {
    expect(isApprovalPending(req(false), withApproved(true))).toBe(true)
    expect(isApprovalPending(req(false), withApproved(null))).toBe(false)
  })

  it('ignores a request for another date', () => {
    expect(isApprovalPending({ ...req(true), date: '2026-03-03' }, withApproved(null))).toBe(false)
  })

  it('the intended state is the pending request, else the record', () => {
    expect(intendedApproval(req(true), withApproved(null))).toBe(true)
    expect(intendedApproval(null, withApproved(true))).toBe(true)
    expect(intendedApproval(null, withApproved(null))).toBe(false)
  })

  describe('once the home box reports it finished with the request', () => {
    const report = (r: ApprovalRequest, over: Partial<PublisherApproval> = {}): PublisherApproval => ({
      date: r.date, approved: r.approved, requested_at: r.requested_at, outcome: 'applied', done: true,
      at: '2026-03-04T21:06:00+00:00', ...over,
    })

    it('an applied sign-off is not pending again after a withdrawal at home', () => {
      // Applied hours ago, then withdrawn (or redrawn) on the local dashboard:
      // the publisher never handles that request again, so it must not read
      // as "waiting for the home box" forever.
      const r = req(true)
      expect(isApprovalPending(r, withApproved(null), report(r))).toBe(false)
      expect(intendedApproval(r, withApproved(null), report(r))).toBe(false)
      expect(isApprovalPending(r, withApproved(null), report(r, { outcome: 'already approved' }))).toBe(false)
    })

    it('an applied withdrawal is not pending again after a sign-off at home', () => {
      const r = req(false)
      expect(isApprovalPending(r, withApproved(true), report(r))).toBe(false)
      expect(intendedApproval(r, withApproved(true), report(r))).toBe(true)
    })

    it('a request refused for good is not pending either', () => {
      const r = req(true)
      expect(isApprovalPending(r, withApproved(null), report(r, { outcome: 'rejected: arrived after the 20:00 ET render' }))).toBe(false)
    })

    it('a failed apply that will be retried is still pending', () => {
      const r = req(true)
      expect(isApprovalPending(r, withApproved(null), report(r, { outcome: 'error: disk full', done: false }))).toBe(true)
    })

    it('a report about an older request does not settle a newer one', () => {
      const r = req(true)
      const older = report({ ...r, requested_at: '2026-03-04T18:00:00+00:00' })
      expect(isApprovalPending(r, withApproved(null), older)).toBe(true)
      expect(intendedApproval(r, withApproved(null), older)).toBe(true)
    })
  })

  it('the render starts at 20:00 ET on the look date, across DST', () => {
    expect(renderStartMs('2026-03-04')).toBe(Date.parse('2026-03-05T01:00:00Z')) // EST
    expect(renderStartMs('2026-07-06')).toBe(Date.parse('2026-07-07T00:00:00Z')) // EDT
  })

  it('stops offering a request once the date’s render has started', () => {
    expect(canRequestApproval('2026-03-04', Date.parse('2026-03-05T00:59:59Z'))).toBe(true)
    expect(canRequestApproval('2026-03-04', Date.parse('2026-03-05T01:00:00Z'))).toBe(false)
    // Tomorrow's look is still open after tonight's render.
    expect(canRequestApproval('2026-03-05', Date.parse('2026-03-05T01:30:00Z'))).toBe(true)
    // Yesterday is inside the Worker's window, but its render is long done.
    expect(canRequestApproval('2026-03-03', Date.parse('2026-03-04T15:00:00Z'))).toBe(false)
  })

  it('accepts dates from ET yesterday to ET today + 3', () => {
    const now = Date.parse('2026-03-04T21:05:00Z') // 16:05 ET on the 4th
    expect(inApprovalWindow('2026-03-03', now)).toBe(true)
    expect(inApprovalWindow('2026-03-07', now)).toBe(true)
    expect(inApprovalWindow('2026-03-02', now)).toBe(false)
    expect(inApprovalWindow('2026-03-08', now)).toBe(false)
  })

  it('prefers the newer of two requests', () => {
    const older = { ...req(true), requested_at: '2026-03-04T20:00:00+00:00' }
    const newer = { ...req(false), requested_at: '2026-03-04T20:01:00+00:00' }
    expect(newestRequest(older, newer)).toBe(newer)
    expect(newestRequest(newer, older)).toBe(newer)
    expect(newestRequest(null, older)).toBe(older)
    expect(newestRequest(undefined, null)).toBeNull()
  })

  it('reports a home-box refusal only for the same request', () => {
    const r = req(true)
    const report = (over: Partial<PublisherApproval>): PublisherApproval => ({
      date: r.date, approved: true, requested_at: r.requested_at, outcome: 'applied', done: true,
      at: '2026-03-04T21:06:00+00:00', ...over,
    })
    expect(homeBoxProblem(r, report({}))).toBeNull()
    expect(homeBoxProblem(r, report({ outcome: 'already approved' }))).toBeNull()
    expect(homeBoxProblem(r, report({ outcome: 'rejected: bad date' }))).toEqual({ state: 'failed', outcome: 'rejected: bad date' })
    expect(homeBoxProblem(r, report({ outcome: 'superseded: changed at home' }))?.state).toBe('failed')
    expect(homeBoxProblem(r, report({ outcome: 'error: disk full', done: false }))?.state).toBe('retrying')
    expect(homeBoxProblem(r, report({ outcome: 'error: disk full', done: true }))?.state).toBe('failed')
    // Same instant written differently is still the same request.
    const zulu = r.requested_at.replace('+00:00', 'Z')
    expect(homeBoxProblem(r, report({ outcome: 'rejected: x', requested_at: zulu }))?.state).toBe('failed')
    // A report about an older request, or another date, says nothing about this one.
    expect(homeBoxProblem(r, report({ outcome: 'rejected: x', requested_at: '2026-03-01T00:00:00+00:00' }))).toBeNull()
    expect(homeBoxProblem(r, report({ outcome: 'rejected: x', date: '2026-03-03' }))).toBeNull()
    expect(homeBoxProblem(null, report({ outcome: 'rejected: x' }))).toBeNull()
    expect(homeBoxProblem(r, null)).toBeNull()
  })
})
