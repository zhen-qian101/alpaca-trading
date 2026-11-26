import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'

import { CSRF_HEADER, CSRF_VALUE, type ApprovalRequest, type LookPresent, type PublisherApproval, type SnapshotResponse } from '../../contract.ts'
import { queryKeys } from '../../lib/queries.ts'
import { approvalFixture, FIXTURE_AS_OF } from '../../test/fixtures.ts'
import { mockApi, snapshotWith } from '../../test/mockApi.ts'
import { freezeTime, renderApp } from '../../test/render.tsx'

// 16:05:30 ET on the session date: inside the approval window, before 20:00 ET.
beforeEach(() => freezeTime(new Date(Date.parse(FIXTURE_AS_OF) + 30_000).toISOString()))

function lookWith(patch: Partial<LookPresent>) {
  const snap = snapshotWith()
  snap.look = { ...(snap.look as LookPresent), ...patch }
  return snap
}

describe('look sign-off', () => {
  it('shows the plate, the keyframe and the unjudged verdict', async () => {
    mockApi()
    renderApp('/look')
    expect(await screen.findByText('Not signed off')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: /Stage keyframe for 2026-03-04/ })).toHaveAttribute(
      'src',
      '/api/look/asset?date=2026-03-04&kind=keyframe&v=0baf886baaac',
    )
    expect(screen.getByRole('img', { name: /Wardrobe reference plate/ })).toHaveAttribute(
      'src',
      '/api/look/asset?date=2026-03-04&kind=plate&v=888ecf1f6db0',
    )
    expect(screen.getByText('Mild and dry: one warm layer, nothing that reads as formal.')).toBeInTheDocument()
    expect(screen.getByText('0.951')).toBeInTheDocument()
  })

  it('counts down to the 20:00 ET render', async () => {
    mockApi()
    renderApp('/look')
    const timer = await screen.findByRole('timer')
    // 21:05:30Z -> 01:00Z next day = 3 h 54 min.
    expect(timer).toHaveTextContent('3 h 54 min')
    expect(screen.getByText(/Melbourne\), in/)).toBeInTheDocument()
  })

  it('signs off after a confirmation, then waits for the home box', async () => {
    const api = mockApi()
    renderApp('/look')
    await userEvent.click(await screen.findByRole('button', { name: 'Sign off this look' }))
    // Nothing is sent until the confirmation.
    expect(api.callsTo('/api/look/approve')).toHaveLength(0)
    const confirm = screen.getByRole('button', { name: 'Confirm sign-off' })
    expect(confirm).toHaveFocus()
    await userEvent.click(confirm)

    await waitFor(() => expect(api.callsTo('/api/look/approve')).toHaveLength(1))
    const [, init] = api.callsTo('/api/look/approve')[0]
    expect(init?.method).toBe('POST')
    const headers = init?.headers as Record<string, string>
    expect(headers['Content-Type']).toBe('application/json')
    expect(headers[CSRF_HEADER]).toBe(CSRF_VALUE)
    expect(JSON.parse(String(init?.body))).toEqual({ date: '2026-03-04', approved: true })

    expect(await screen.findByText('Sign-off requested', { selector: '.badge' })).toBeInTheDocument()
    expect(screen.getByText(/waiting for the home box to apply it/, { selector: 'p:not([role])' })).toBeInTheDocument()
    expect(screen.getByText(/Requested by reviewer@example.com/)).toBeInTheDocument()
    // F11: the confirmation closed under the keyboard; focus is back on the button that opened it.
    const trigger = screen.getByRole('button', { name: 'Cancel the request' })
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('can back out of the confirmation without sending anything, keeping focus', async () => {
    const api = mockApi()
    renderApp('/look')
    await userEvent.click(await screen.findByRole('button', { name: 'Sign off this look' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: 'Sign off this look' })).toHaveFocus()
    expect(api.callsTo('/api/look/approve')).toHaveLength(0)

    await userEvent.keyboard('{Enter}') // the same button opens it again...
    expect(screen.getByRole('button', { name: 'Confirm sign-off' })).toHaveFocus()
    await userEvent.keyboard('{Escape}') // ...and Escape closes it back onto that button
    expect(screen.getByRole('button', { name: 'Sign off this look' })).toHaveFocus()
    expect(api.callsTo('/api/look/approve')).toHaveLength(0)
  })

  // F7: the verdict's ages tick every second; a live region around them made
  // a screen reader repeat the whole verdict. Only the verdict itself is live.
  it('keeps ticking ages out of live regions and announces only a verdict change', async () => {
    mockApi()
    renderApp('/look')
    await screen.findByText('Not signed off', { selector: '.badge' })
    const status = screen.getAllByRole('status').find((el) => el.closest('[aria-labelledby="verdict-title"]'))!
    expect(status).toHaveTextContent('') // the verdict as the page loaded is not read out
    await userEvent.click(screen.getByRole('button', { name: 'Sign off this look' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm sign-off' }))
    await waitFor(() => expect(status).toHaveTextContent(/^Sign-off requested\. Sign-off requested — waiting/))
    const age = screen.getByText(/Requested by reviewer@example.com/)
    expect(age.closest('[aria-live]:not([aria-live="off"])')).toBeNull()
  })

  it('shows a pending request from the snapshot until the look reflects it', async () => {
    const snap = snapshotWith({ approval_request: approvalFixture })
    mockApi({ '/api/snapshot': { status: 200, body: snap } })
    renderApp('/look')
    expect(await screen.findByText('Sign-off requested')).toBeInTheDocument()
  })

  it('drops the pending state once the home box has applied it', async () => {
    const snap = lookWith({ approved: true, approved_at: '2026-03-04T21:00:00+00:00', renders_with: 'look' })
    snap.approval_request = approvalFixture
    mockApi({ '/api/snapshot': { status: 200, body: snap } })
    renderApp('/look')
    expect(await screen.findByText('Signed off')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Withdraw sign-off' })).toBeInTheDocument()
  })

  it('withdraws a sign-off', async () => {
    const api = mockApi({ '/api/snapshot': { status: 200, body: lookWith({ approved: true }) } })
    renderApp('/look')
    await userEvent.click(await screen.findByRole('button', { name: 'Withdraw sign-off' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm withdrawal' }))
    await waitFor(() => expect(api.callsTo('/api/look/approve')).toHaveLength(1))
    expect(JSON.parse(String(api.callsTo('/api/look/approve')[0][1]?.body))).toEqual({ date: '2026-03-04', approved: false })
    expect(await screen.findByText('Withdrawal requested')).toBeInTheDocument()
  })

  it('says why when the request is refused', async () => {
    mockApi({ '/api/look/approve': { status: 400, body: { error: 'date outside the approval window' } } })
    renderApp('/look')
    await userEvent.click(await screen.findByRole('button', { name: 'Sign off this look' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm sign-off' }))
    const alert = await screen.findByText(/The request was not saved/)
    expect(alert.closest('[role="alert"]')).toHaveTextContent('date outside the approval window')
    expect(screen.getByText('Not signed off')).toBeInTheDocument()
  })

  it('distinguishes a refused look from an unjudged one', async () => {
    mockApi({ '/api/snapshot': { status: 200, body: lookWith({ approved: false }) } })
    renderApp('/look')
    expect(await screen.findByText('Refused')).toBeInTheDocument()
  })

  it('has nothing to review when the stylist is off', async () => {
    mockApi({ '/api/snapshot': { status: 200, body: lookWith({ stylist_enabled: false }) } })
    renderApp('/look')
    expect(await screen.findByText(/The stylist is switched off/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Sign off/ })).not.toBeInTheDocument()
  })

  it('says when no look was built', async () => {
    const snap = snapshotWith()
    const base = snap.look as LookPresent
    snap.look = {
      date: base.date, session_date: base.session_date, host: base.host, host_json: base.host_json,
      stylist_enabled: true, locked_keyframe: null, dates: [], exists: false, renders_with: 'locked',
    }
    mockApi({ '/api/snapshot': { status: 200, body: snap } })
    renderApp('/look')
    const note = await screen.findByText(/No look was built for/)
    expect(within(note.closest('.panel')!).getByRole('timer')).toBeInTheDocument()
  })

  it('says when the home box would not apply the request', async () => {
    const snap = snapshotWith({ approval_request: approvalFixture })
    snap.publisher = {
      ...snap.publisher,
      approval: {
        date: approvalFixture.date, approved: true, requested_at: approvalFixture.requested_at,
        outcome: 'superseded: the look changed at home after this request', done: true, at: FIXTURE_AS_OF,
      },
    }
    mockApi({ '/api/snapshot': { status: 200, body: snap } })
    renderApp('/look')
    expect(await screen.findByText('Not applied')).toBeInTheDocument()
    expect(screen.getByText(/did not apply this sign-off \(superseded/)).toBeInTheDocument()
  })
  it('shows a same-date redraw at once, and retries an image that failed', async () => {
    mockApi()
    const { client } = renderApp('/look')
    const keyframe = await screen.findByRole('img', { name: /Stage keyframe for 2026-03-04/ })
    // A poll landed while the image was not in the bucket yet.
    fireEvent.error(keyframe)
    expect(await screen.findByText(/The image is not available yet/)).toBeInTheDocument()

    // The stylist redrew the look: new bytes, so the publisher sends a new URL.
    const redrawn = snapshotWith()
    const look = redrawn.look as LookPresent
    redrawn.look = { ...look, keyframe_url: '/api/look/asset?date=2026-03-04&kind=keyframe&v=aaaaaaaaaaaa', style: { ...look.style!, slug: 'redrawn-olive-overshirt' } }
    act(() => client.setQueryData<SnapshotResponse>(queryKeys.snapshot, redrawn))
    expect(await screen.findByRole('img', { name: /Stage keyframe for 2026-03-04/ })).toHaveAttribute(
      'src',
      '/api/look/asset?date=2026-03-04&kind=keyframe&v=aaaaaaaaaaaa',
    )
    expect(screen.queryByText(/The image is not available yet/)).not.toBeInTheDocument()
    expect(screen.getByRole('img', { name: /keyframe's face/ })).toHaveAttribute(
      'src',
      '/api/look/asset?date=2026-03-04&kind=keyframe&v=aaaaaaaaaaaa',
    )
  })

  describe('after the home box has finished with a request', () => {
    // A request from three hours ago that the home box applied.
    const threeHoursAgo = new Date(Date.parse(FIXTURE_AS_OF) - 3 * 3600_000).toISOString().replace('.000Z', '+00:00')
    const handled = (approved: boolean, lookApproved: boolean | null) => {
      const request: ApprovalRequest = { ...approvalFixture, approved, requested_at: threeHoursAgo }
      const snap = lookWith({ approved: lookApproved, renders_with: lookApproved ? 'look' : 'locked' })
      snap.approval_request = request
      const report: PublisherApproval = {
        date: request.date, approved, requested_at: threeHoursAgo, outcome: 'applied', done: true, at: threeHoursAgo,
      }
      snap.publisher = { ...snap.publisher, approval: report }
      return snap
    }

    it('a sign-off withdrawn at home since then is not "waiting" again', async () => {
      mockApi({ '/api/snapshot': { status: 200, body: handled(true, null) } })
      renderApp('/look')
      expect(await screen.findByText('Not signed off')).toBeInTheDocument()
      expect(screen.queryByText(/waiting for the home box/)).not.toBeInTheDocument()
      expect(screen.queryByText(/Requested by/)).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Sign off this look' })).toBeEnabled()
    })

    it('a withdrawal followed by a sign-off at home reads as signed off', async () => {
      mockApi({ '/api/snapshot': { status: 200, body: handled(false, true) } })
      renderApp('/look')
      expect(await screen.findByText('Signed off')).toBeInTheDocument()
      expect(screen.queryByText('Withdrawal requested')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Withdraw sign-off' })).toBeInTheDocument()
    })
  })

  it('a refused request offers to send it again rather than to cancel it', async () => {
    const snap = snapshotWith({ approval_request: approvalFixture })
    snap.publisher = {
      ...snap.publisher,
      approval: {
        date: approvalFixture.date, approved: true, requested_at: approvalFixture.requested_at,
        outcome: 'superseded: the look changed at home after this request', done: true, at: FIXTURE_AS_OF,
      },
    }
    mockApi({ '/api/snapshot': { status: 200, body: snap } })
    renderApp('/look')
    expect(await screen.findByText('Not applied')).toBeInTheDocument()
    expect(screen.getByText(/Requested by reviewer@example.com/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign off this look' })).toBeEnabled()
  })

  it('warns in the confirmation when the home box has gone quiet', async () => {
    // 16:15 ET: ten minutes after the snapshot, so it is stale; still before the render.
    freezeTime(new Date(Date.parse(FIXTURE_AS_OF) + 10 * 60_000).toISOString())
    mockApi()
    renderApp('/look')
    await userEvent.click(await screen.findByRole('button', { name: 'Sign off this look' }))
    const group = screen.getByRole('group', { name: /Sign off the/ })
    expect(within(group).getByText(/has not reported since/)).toHaveTextContent(
      'The home box has not reported since 16:05 ET. The request is kept, but it is applied only when the home box reports again, and not at all if that is after 20:00 ET.',
    )
  })

  describe('after the 20:00 ET render has started', () => {
    // 20:30 ET on the look date (2026-03-05T01:30Z), with a fresh snapshot.
    const AFTER = '2026-03-05T01:30:00Z'
    const fresh = (patch: Partial<LookPresent> = {}) => {
      const snap = lookWith(patch)
      snap.as_of = '2026-03-05T01:29:40+00:00'
      return snap
    }
    beforeEach(() => freezeTime(AFTER))

    it('says in the past tense what the render used, and closes sign-off', async () => {
      mockApi({ '/api/snapshot': { status: 200, body: fresh() } })
      renderApp('/look')
      expect(await screen.findByText('Not signed off')).toBeInTheDocument()
      expect(screen.getByText('Not signed off, so the Wed, 4 Mar 2026 render used the locked keyframe.')).toBeInTheDocument()
      expect(screen.queryByText(/Sign off to use/)).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Sign off this look' })).toBeDisabled()
      expect(screen.getByText(/Sign-off from here closed when the 20:00 ET render started/)).toBeInTheDocument()
      expect(screen.getByText(/render for this date has started or finished/)).toBeInTheDocument()
    })

    it('a look signed off before the render is what the video used', async () => {
      mockApi({ '/api/snapshot': { status: 200, body: fresh({ approved: true, approved_at: '2026-03-04T23:00:00+00:00', renders_with: 'look' }) } })
      renderApp('/look')
      expect(await screen.findByText('Signed off: the Wed, 4 Mar 2026 render used this look.')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Withdraw sign-off' })).toBeDisabled()
    })

    it('a look signed off after 20:00 ET did not make it into the video', async () => {
      mockApi({ '/api/snapshot': { status: 200, body: fresh({ approved: true, approved_at: '2026-03-05T01:10:00+00:00', renders_with: 'look' }) } })
      renderApp('/look')
      expect(await screen.findByText('Signed off late')).toBeInTheDocument()
      expect(screen.getByText(/so the Wed, 4 Mar 2026 video used the locked keyframe/)).toBeInTheDocument()
    })

    it('a request the home box had not applied by 20:00 ET will not be', async () => {
      const snap = fresh()
      snap.approval_request = { ...approvalFixture, requested_at: '2026-03-05T00:50:00+00:00' }
      mockApi({ '/api/snapshot': { status: 200, body: snap } })
      renderApp('/look')
      expect(await screen.findByText('Sign-off requested')).toBeInTheDocument()
      expect(screen.getByText(/started before the home box applied this sign-off, so it will not be applied/)).toBeInTheDocument()
      expect(screen.queryByText(/picks requests up on its next report/)).not.toBeInTheDocument()
    })

    it('shows the home box refusing a late request', async () => {
      const snap = fresh()
      const requested_at = '2026-03-05T00:50:00+00:00'
      snap.approval_request = { ...approvalFixture, requested_at }
      snap.publisher = {
        ...snap.publisher,
        approval: {
          date: '2026-03-04', approved: true, requested_at, done: true, at: '2026-03-05T01:05:00+00:00',
          outcome: 'rejected: arrived after the 20:00 ET render of 2026-03-04 started; sign off on the local dashboard if a re-run should use it',
        },
      }
      mockApi({ '/api/snapshot': { status: 200, body: snap } })
      renderApp('/look')
      expect(await screen.findByText('Not applied')).toBeInTheDocument()
      expect(screen.getByText(/arrived after the 20:00 ET render .*so the render used the locked keyframe\.$/)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Sign off this look' })).toBeDisabled()
    })
  })
})
