import { act, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { Edition, History } from '../../contract.ts'
import { queryKeys } from '../../lib/queries.ts'
import { historyFixture } from '../../test/fixtures.ts'
import { mockApi } from '../../test/mockApi.ts'
import { renderApp } from '../../test/render.tsx'

const video = () => document.querySelector('video')!

describe('briefing', () => {
  it('lists every edition and opens the newest', async () => {
    mockApi()
    renderApp('/briefing')
    expect(await screen.findByRole('heading', { level: 2, name: 'Semis carry a narrow tape higher' })).toBeInTheDocument()
    const list = screen.getByRole('region', { name: 'Editions' })
    expect(within(list).getAllByRole('button')).toHaveLength(3)
    expect(within(list).getByRole('button', { name: /Post-market recap/ })).toHaveAttribute('aria-current', 'true')
    expect(video()).toHaveAttribute('src', historyFixture.briefings[0].briefing)
    expect(video()).toHaveAttribute('poster', historyFixture.briefings[0].cover)
  })

  it('switches to the zh cut, its poster and title', async () => {
    mockApi()
    renderApp('/briefing')
    await screen.findByRole('group', { name: 'Narration language' })
    await userEvent.click(screen.getByRole('button', { name: '中文' }))
    expect(video()).toHaveAttribute('src', historyFixture.briefings[0].briefing_zh)
    expect(video()).toHaveAttribute('poster', historyFixture.briefings[0].cover_zh)
    expect(screen.getByRole('heading', { level: 2, name: '半导体独撑大盘' })).toBeInTheDocument()
  })

  it('offers no language toggle for an edition without a zh cut', async () => {
    mockApi()
    renderApp('/briefing')
    await userEvent.click(await screen.findByRole('button', { name: /Midday briefing/ }))
    expect(screen.queryByRole('group', { name: 'Narration language' })).not.toBeInTheDocument()
    expect(video()).toHaveAttribute('src', historyFixture.briefings[1].briefing)
  })

  it('lists chapters and seeks the video to one', async () => {
    mockApi()
    renderApp('/briefing?edition=2026-03-01/video')
    expect(await screen.findByText('Jump to a chapter')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /The Hare/ }))
    expect(video().currentTime).toBe(48)
  })

  it('marks estimated chapter starts as approximate', async () => {
    mockApi()
    renderApp('/briefing')
    expect(await screen.findByText('Start times estimated from scene lengths')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /DESK/ })).toHaveTextContent('≈0:34')
  })

  it('invites a check-back when nothing is published', async () => {
    mockApi({ '/media/history.json': { status: 200, body: { generated_at: '2026-03-04T22:31:07+00:00', current: null, briefings: [] } } })
    renderApp('/briefing')
    expect(await screen.findByText('No recap has been published yet.')).toBeInTheDocument()
  })

  it('treats a missing history.json as nothing published yet', async () => {
    mockApi({ '/media/history.json': { status: 404, body: { error: 'not found' } } })
    renderApp('/briefing')
    expect(await screen.findByText('No recap has been published yet.')).toBeInTheDocument()
  })

  it('explains a load failure and offers a retry', async () => {
    const api = mockApi({ '/media/history.json': { status: 500, body: { error: 'R2 unavailable' } } })
    renderApp('/briefing')
    expect(await screen.findByText(/The list of recaps did not load/)).toBeInTheDocument()
    expect(screen.getByText(/R2 unavailable/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(api.callsTo('/media/history.json').length).toBe(2)
  })

  it('shows the signed-out state on 401', async () => {
    mockApi({
      '/media/history.json': { status: 401, body: { error: 'unauthenticated' } },
      '/api/snapshot': { status: 401, body: { error: 'unauthenticated' } },
    })
    renderApp('/briefing')
    expect(await screen.findByText(/The list of recaps did not load/)).toBeInTheDocument()
    expect(screen.getAllByText(/Signed out — reload to sign in via Cloudflare Access/).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })
  it('keeps playing the edition on screen when a newer one is published', async () => {
    mockApi()
    const { client } = renderApp('/briefing')
    await screen.findByRole('heading', { level: 2, name: 'Semis carry a narrow tape higher' })
    const playing = video()
    expect(playing).toHaveAttribute('src', historyFixture.briefings[0].briefing)

    // The 5-minute history poll brings tonight's recap.
    const tonight: Edition = {
      ...historyFixture.briefings[0],
      date: '2026-03-05',
      generated_at: '2026-03-06T01:40:00+00:00',
      title: 'A later synthetic recap',
      briefing: '/media/2026-03-05/video/post-market-recap_2026-03-05.mp4',
    }
    const updated: History = { ...historyFixture, current: '2026-03-05', briefings: [tonight, ...historyFixture.briefings] }
    act(() => client.setQueryData<History>(queryKeys.history, updated))

    const note = await screen.findByRole('status')
    expect(note).toHaveTextContent('New edition: Post-market recap for Thu, 5 Mar 2026.')
    // Same <video> element, same source: nothing was swapped mid-playback.
    expect(video()).toBe(playing)
    expect(playing).toHaveAttribute('src', historyFixture.briefings[0].briefing)
    expect(screen.getByRole('heading', { level: 2, name: 'Semis carry a narrow tape higher' })).toBeInTheDocument()

    await userEvent.click(within(note).getByRole('button', { name: 'Watch it' }))
    expect(video()).toHaveAttribute('src', tonight.briefing)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
