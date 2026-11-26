import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { FIXTURE_AS_OF } from './test/fixtures.ts'
import { mockApi, snapshotWith } from './test/mockApi.ts'
import { freezeTime, renderApp } from './test/render.tsx'

// The canvas chart cannot run in jsdom; the shell tests do not need it.
vi.mock('./features/terminal/chart/signalChart.ts', () => ({
  SignalChart: class {
    setData() {}
    setZone() {}
    setPalette() {}
    destroy() {}
  },
  readPalette: () => ({}),
}))

const AS_OF_MS = Date.parse(FIXTURE_AS_OF)
const at = (ms: number) => new Date(AS_OF_MS + ms).toISOString()

describe('app shell', () => {
  it('has one navigation with the three pages and marks the current one', async () => {
    mockApi()
    freezeTime(at(42_000))
    renderApp('/briefing')
    const nav = screen.getByRole('navigation', { name: 'Main' })
    const links = within(nav).getAllByRole('link')
    expect(links.map((l) => l.textContent)).toEqual(['Terminal', 'Briefing', 'Look'])
    expect(within(nav).getByRole('link', { name: 'Briefing' })).toHaveAttribute('aria-current', 'page')
    expect(await screen.findByRole('heading', { level: 1, name: 'Briefing' })).toBeInTheDocument()
  })

  it('shows the snapshot age on every page', async () => {
    mockApi()
    freezeTime(at(42_000))
    renderApp('/look')
    expect(await screen.findByText('as of 42 s ago')).toBeInTheDocument()
  })

  it('says nothing about staleness while the report is under 3 minutes old', async () => {
    mockApi()
    freezeTime(at(60_000))
    renderApp('/')
    expect(await screen.findByText('as of 60 s ago')).toBeInTheDocument()
    expect(screen.queryByText(/Home box not reporting/)).not.toBeInTheDocument()
    expect(screen.queryByText('Stale')).not.toBeInTheDocument()
  })

  it('raises the banner and marks panels stale once the report is over 3 minutes old', async () => {
    mockApi()
    freezeTime(at(4 * 60_000))
    renderApp('/')
    const banner = await screen.findByRole('alert')
    expect(banner).toHaveTextContent('Home box not reporting since 4 Mar 16:05 ET.')
    expect(screen.getByText('4 min without a report')).toBeInTheDocument()
    // Every snapshot panel says so itself, not only the banner.
    expect(screen.getAllByText('Stale').length).toBeGreaterThanOrEqual(5)
    expect(document.getElementById('positions')).toHaveAttribute('data-stale', 'true')
  })

  // F9: polling pauses while the tab is hidden but the clock does not. On
  // return the cached snapshot was minutes old, so the outage alert fired and
  // vanished a round trip later.
  describe('returning to a tab after a while', () => {
    let visibility: DocumentVisibilityState = 'visible'
    const setVisibility = (next: DocumentVisibilityState) => {
      visibility = next
      act(() => {
        document.dispatchEvent(new Event('visibilitychange', { bubbles: true }))
      })
    }

    function holdTheRefetch() {
      let calls = 0
      let answer: (asOf: string) => void = () => {}
      mockApi({
        '/api/snapshot': () => {
          calls += 1
          if (calls === 1) return { status: 200, body: snapshotWith() }
          return new Promise((resolve) => {
            answer = (asOf) => resolve({ status: 200, body: snapshotWith({ as_of: asOf }) })
          })
        },
      })
      return { answer: (asOf: string) => act(() => answer(asOf)), calls: () => calls }
    }

    async function leaveAndComeBack() {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility })
      freezeTime(at(30_000))
      renderApp('/')
      expect(await screen.findByText('as of 30 s ago')).toBeInTheDocument()
      setVisibility('hidden')
      vi.setSystemTime(new Date(at(6 * 60_000)))
      setVisibility('visible')
    }

    afterEach(() => {
      visibility = 'visible'
      delete (document as { visibilityState?: unknown }).visibilityState
    })

    it('checks quietly instead of alerting, then shows the fresh report', async () => {
      const api = holdTheRefetch()
      await leaveAndComeBack()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.getByText(/checking…/)).toBeInTheDocument()
      expect(screen.queryAllByText('Stale')).toHaveLength(0)
      await waitFor(() => expect(api.calls()).toBe(2)) // the return asked again
      api.answer(at(6 * 60_000 - 20_000))
      expect(await screen.findByText('as of 20 s ago')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it('still alerts when the refetch confirms the box went quiet', async () => {
      const api = holdTheRefetch()
      await leaveAndComeBack()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      await waitFor(() => expect(api.calls()).toBe(2))
      api.answer(FIXTURE_AS_OF)
      expect(await screen.findByRole('alert')).toHaveTextContent('Home box not reporting since 4 Mar 16:05 ET.')
    })
  })

  it('reports the banner time in the chosen zone', async () => {
    mockApi()
    freezeTime(at(10 * 60_000))
    renderApp('/')
    await screen.findByRole('alert')
    await userEvent.click(screen.getAllByRole('button', { name: 'Melbourne' })[0])
    expect(screen.getByRole('alert')).toHaveTextContent('Home box not reporting since 5 Mar 08:05 Melbourne.')
  })

  it('shows the signed-out state on a 401 and does not retry it', async () => {
    const api = mockApi({ '/api/snapshot': { status: 401, body: { error: 'missing Access token' } } })
    renderApp('/')
    expect(await screen.findByText('Signed out — reload to sign in via Cloudflare Access')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument()
    await new Promise((r) => setTimeout(r, 50))
    expect(api.callsTo('/api/snapshot')).toHaveLength(1)
  })

  it('says exactly what failed when the API is down on first load', async () => {
    mockApi({ '/api/snapshot': { status: 502, body: { error: 'R2 read failed' } } })
    renderApp('/')
    expect(await screen.findByText(/The dashboard API answered 502: R2 read failed/)).toBeInTheDocument()
  })

  it('keeps old numbers but says the refresh failed', async () => {
    let healthy = true
    mockApi({
      '/api/snapshot': () =>
        healthy ? { status: 200, body: snapshotWith() } : { status: 503, body: { error: 'worker overloaded' } },
    })
    freezeTime(at(20_000))
    const { client } = renderApp('/')
    expect(await screen.findByText('$102,161.29')).toBeInTheDocument()
    healthy = false
    await client.refetchQueries({ queryKey: ['snapshot'] })
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('The last refresh failed.'))
    expect(screen.getByRole('alert')).toHaveTextContent('worker overloaded')
    expect(screen.getByText('$102,161.29')).toBeInTheDocument()
    expect(document.getElementById('positions')).toHaveAttribute('data-stale', 'true')
  })

  it('refuses to pretend a snapshot in an unknown format is fine', async () => {
    mockApi({ '/api/snapshot': { status: 200, body: { ...snapshotWith(), schema: 2 } } })
    freezeTime(at(10_000))
    renderApp('/')
    expect(await screen.findByRole('alert')).toHaveTextContent('The home box sent snapshot format 2; this page reads format 1.')
  })

  it('switches theme and remembers it', async () => {
    mockApi()
    renderApp('/')
    await userEvent.click(screen.getByRole('button', { name: 'Switch to light theme' }))
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(window.localStorage.getItem('alpaca-dashboard:theme')).toBe('light')
    expect(screen.getByRole('button', { name: 'Switch to dark theme' })).toBeInTheDocument()
  })

  it('has a not-found page inside the shell', async () => {
    mockApi()
    renderApp('/nope')
    expect(await screen.findByRole('heading', { name: 'Nothing at this address' })).toBeInTheDocument()
  })
})
