import { act, fireEvent, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { Edition, History } from '../../contract.ts'
import { queryKeys } from '../../lib/queries.ts'
import { historyFixture } from '../../test/fixtures.ts'
import { mockApi } from '../../test/mockApi.ts'
import { renderApp } from '../../test/render.tsx'

const [recap, slideshow, story] = historyFixture.briefings

const video = () => document.querySelector('video')
const frame = () => document.querySelector('iframe')
const playButton = () => screen.getByRole('button', { name: /Loads the YouTube player/ })

/** Every URL an element on the page would make the browser fetch (links followed only on click are not). */
function resourceUrls(): string[] {
  const urls: string[] = []
  document.querySelectorAll('[src], [srcset], [poster], [data], link[href]').forEach((el) => {
    for (const attr of ['src', 'srcset', 'poster', 'data', 'href']) {
      const v = el.getAttribute(attr)
      if (v && !(el.tagName === 'A' && attr === 'href')) urls.push(v)
    }
  })
  return urls
}

/** True when nothing on the page, and no fetch, has touched YouTube or Google. */
function nothingFromYoutube(api: ReturnType<typeof mockApi>): boolean {
  const touched = [...resourceUrls(), ...api.calls().map(([path]) => path)]
  return touched.every((u) => !/youtube|ytimg|google/i.test(u))
}

describe('briefing', () => {
  it('lists every edition and opens the newest behind a click-to-play cover', async () => {
    const api = mockApi()
    renderApp('/briefing')
    expect(await screen.findByRole('heading', { level: 2, name: 'Semis carry a narrow tape higher' })).toBeInTheDocument()
    const list = screen.getByRole('region', { name: 'Editions' })
    expect(within(list).getAllByRole('button')).toHaveLength(3)
    expect(within(list).getByRole('button', { name: /Post-market recap/ })).toHaveAttribute('aria-current', 'true')

    // Before the click: the R2 cover as a play button, no player, no YouTube request.
    const play = playButton()
    expect(play).toHaveAccessibleName('Play Post-market recap for Wed, 4 Mar 2026, 2:22. Loads the YouTube player.')
    expect(play.querySelector('img')).toHaveAttribute('src', recap.cover)
    expect(frame()).toBeNull()
    expect(video()).toBeNull()
    expect(nothingFromYoutube(api)).toBe(true)
  })

  it('loads the no-cookie YouTube player only when play is pressed', async () => {
    const api = mockApi()
    renderApp('/briefing')
    await userEvent.click(await screen.findByRole('button', { name: /Loads the YouTube player/ }))

    const iframe = frame()!
    expect(iframe).toHaveAttribute('src', 'https://www.youtube-nocookie.com/embed/FAKEyt00001?autoplay=1&rel=0&playsinline=1')
    expect(iframe).toHaveAttribute('title', 'Post-market recap for Wed, 4 Mar 2026 (YouTube player)')
    expect(iframe.getAttribute('allow')).toContain('autoplay')
    // The page sends no referrer; YouTube's player needs the origin to play.
    expect(iframe).toHaveAttribute('referrerpolicy', 'strict-origin-when-cross-origin')
    // The play button is gone; focus moves into the player instead of the page top.
    expect(screen.queryByRole('button', { name: /Loads the YouTube player/ })).not.toBeInTheDocument()
    expect(document.activeElement).toBe(iframe)
    // Still nothing fetched from YouTube by the page itself: only the frame.
    expect(api.calls().every(([path]) => !path.includes('youtube'))).toBe(true)
  })

  it('links the YouTube edition out without loading anything from it', async () => {
    mockApi()
    renderApp('/briefing')
    const link = await screen.findByRole('link', { name: 'Open on YouTube' })
    expect(link).toHaveAttribute('href', 'https://www.youtube.com/watch?v=FAKEyt00001')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('shows a neutral placeholder when a YouTube edition has no cover', async () => {
    const api = mockApi()
    renderApp('/briefing?edition=2026-03-01/video')
    const play = await screen.findByRole('button', { name: /Play Weekend recap for Sun, 1 Mar 2026/ })
    expect(play.querySelector('img')).toBeNull()
    expect(play).toHaveTextContent('Weekend recap')
    expect(nothingFromYoutube(api)).toBe(true)
  })

  it('falls back to the placeholder when the cover does not load', async () => {
    mockApi()
    renderApp('/briefing')
    const img = (await screen.findByRole('button', { name: /Loads the YouTube player/ })).querySelector('img')!
    fireEvent.error(img)
    expect(playButton().querySelector('img')).toBeNull()
    expect(playButton()).toHaveTextContent('Post-market recap')
  })

  it('jumps a YouTube edition to a chapter by loading the embed at its start', async () => {
    const api = mockApi()
    renderApp('/briefing?edition=2026-03-01/video')
    expect(await screen.findByText('Jump to a chapter')).toBeInTheDocument()
    expect(nothingFromYoutube(api)).toBe(true)

    const hare = screen.getByRole('button', { name: /The Hare/ })
    await userEvent.click(hare)
    const first = frame()!
    expect(first).toHaveAttribute('src', 'https://www.youtube-nocookie.com/embed/FAKEyt00002?autoplay=1&rel=0&playsinline=1&start=48')
    // The chapter list keeps focus, so the next chapter is one key away.
    expect(document.activeElement).toBe(hare)

    // Another jump is a fresh player (no back-button entry inside the frame).
    await userEvent.click(screen.getByRole('button', { name: /The Tortoise/ }))
    expect(frame()).not.toBe(first)
    expect(frame()).toHaveAttribute('src', 'https://www.youtube-nocookie.com/embed/FAKEyt00002?autoplay=1&rel=0&playsinline=1')
  })

  it('jumps estimated chapters to whole seconds', async () => {
    mockApi()
    renderApp('/briefing')
    expect(await screen.findByText('Start times estimated from scene lengths')).toBeInTheDocument()
    const desk = screen.getByRole('button', { name: /DESK/ })
    expect(desk).toHaveTextContent('≈0:34')
    await userEvent.click(desk)
    expect(frame()?.getAttribute('src')).toMatch(/&start=34$/)
  })

  it('plays an edition that did not reach YouTube from its uploaded mp4', async () => {
    const api = mockApi()
    renderApp('/briefing')
    await userEvent.click(await screen.findByRole('button', { name: /Midday briefing/ }))
    expect(video()).toHaveAttribute('src', slideshow.briefing)
    expect(video()).toHaveAttribute('poster', slideshow.cover)
    expect(frame()).toBeNull()
    expect(screen.queryByRole('button', { name: /Loads the YouTube player/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Open on YouTube' })).not.toBeInTheDocument()
    expect(screen.getByText(/Not on YouTube: playing the copy uploaded from home/)).toBeInTheDocument()
    expect(nothingFromYoutube(api)).toBe(true)
  })

  it('seeks the fallback video to a chapter', async () => {
    mockApi()
    renderApp(`/briefing?edition=${slideshow.date}/${slideshow.slot}`)
    expect(await screen.findByText('Start times estimated from scene lengths')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /ORBT/ }))
    expect(video()!.currentTime).toBeCloseTo(22)
  })

  it('offers no Chinese anywhere, whatever language was chosen before', async () => {
    window.localStorage.setItem('alpaca-dashboard:lang', 'zh')
    mockApi()
    renderApp('/briefing')
    await screen.findByRole('heading', { level: 2, name: 'Semis carry a narrow tape higher' })
    for (const ed of historyFixture.briefings) {
      await userEvent.click(within(screen.getByRole('region', { name: 'Editions' })).getByRole('button', { name: new RegExp(ed.label!) }))
      expect(screen.queryByRole('group', { name: 'Narration language' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: '中文' })).not.toBeInTheDocument()
      expect(document.body.textContent).not.toMatch(/[一-鿿]|EN \+/)
      expect(document.querySelector('[lang="zh"]')).toBeNull()
      expect(resourceUrls().some((u) => /_zh/.test(u))).toBe(false)
    }
  })

  it('leaves out an edition with neither a YouTube id nor an uploaded mp4', async () => {
    const broken: Edition = {
      ...recap,
      date: '2026-03-05',
      generated_at: '2026-03-05T22:30:00+00:00',
      title: 'A synthetic recap with nothing to play',
      youtube_id: undefined,
    }
    const badId: Edition = { ...broken, slot: 'slideshow-late', youtube_id: 'not-an-id' }
    mockApi({
      '/media/history.json': {
        status: 200,
        body: { ...historyFixture, current: '2026-03-05', briefings: [broken, badId, ...historyFixture.briefings] },
      },
    })
    renderApp('/briefing?edition=2026-03-05/video')
    // The pinned edition is not playable, so the newest playable one opens.
    expect(await screen.findByRole('heading', { level: 2, name: 'Semis carry a narrow tape higher' })).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Editions' })).getAllByRole('button')).toHaveLength(3)
    expect(screen.queryByText('A synthetic recap with nothing to play')).not.toBeInTheDocument()
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
    await userEvent.click(await screen.findByRole('button', { name: /Loads the YouTube player/ }))
    const playing = frame()!
    expect(playing.getAttribute('src')).toContain('/embed/FAKEyt00001?')

    // The 5-minute history poll brings tonight's recap.
    const tonight: Edition = {
      ...recap,
      date: '2026-03-05',
      generated_at: '2026-03-06T01:40:00+00:00',
      title: 'A later synthetic recap',
      youtube_id: 'FAKEyt00009',
    }
    const updated: History = { ...historyFixture, current: '2026-03-05', briefings: [tonight, ...historyFixture.briefings] }
    act(() => client.setQueryData<History>(queryKeys.history, updated))

    const note = await screen.findByRole('status')
    expect(note).toHaveTextContent('New edition: Post-market recap for Thu, 5 Mar 2026.')
    // Same player element, same video: nothing was swapped mid-playback.
    expect(frame()).toBe(playing)
    expect(playing.getAttribute('src')).toContain('/embed/FAKEyt00001?')
    expect(screen.getByRole('heading', { level: 2, name: 'Semis carry a narrow tape higher' })).toBeInTheDocument()

    await userEvent.click(within(note).getByRole('button', { name: 'Watch it' }))
    // The new edition opens on its own play button: nothing loads until pressed.
    expect(frame()).toBeNull()
    expect(screen.getByRole('heading', { level: 2, name: 'A later synthetic recap' })).toBeInTheDocument()
    await userEvent.click(playButton())
    expect(frame()?.getAttribute('src')).toContain('/embed/FAKEyt00009?')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('keeps the fallback video playing when a newer edition is published', async () => {
    mockApi()
    const { client } = renderApp(`/briefing?edition=${slideshow.date}/${slideshow.slot}`)
    await screen.findByRole('heading', { level: 2, name: slideshow.title! })
    const playing = video()!
    const tonight: Edition = { ...story, date: '2026-03-05', generated_at: '2026-03-06T01:40:00+00:00', youtube_id: 'FAKEyt00009' }
    act(() =>
      client.setQueryData<History>(queryKeys.history, { ...historyFixture, briefings: [tonight, ...historyFixture.briefings] }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent('New edition: Weekend recap for Thu, 5 Mar 2026.')
    expect(video()).toBe(playing)
    expect(playing).toHaveAttribute('src', slideshow.briefing)
  })
})
