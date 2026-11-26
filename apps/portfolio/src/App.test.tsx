import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import fixture from '../fixtures/portfolio.json'
import App from './App.tsx'
import { MINUS, signedPct } from './lib/format.ts'
import { THEME_KEY } from './lib/themeContext.ts'
import { chartCalls } from './test/chartMock.ts'

const PAGES = [
  { path: '/', heading: /this system has traded/i },
  { path: '/trades', heading: 'Trades' },
  { path: '/strategy', heading: 'Strategy & risk' },
  { path: '/how-its-built', heading: 'How it’s built' },
  { path: '/recaps', heading: 'Recaps' },
] as const

/** Serves `body` as /data/portfolio.json and records every request the page makes. */
function serve(body: unknown = fixture, init: ResponseInit = { status: 200, headers: { 'content-type': 'application/json' } }) {
  const requests: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      requests.push(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url)
      return new Response(typeof body === 'string' ? body : JSON.stringify(body), init)
    }),
  )
  const xhrOpen = vi.spyOn(XMLHttpRequest.prototype, 'open').mockImplementation(function (_m: string, url: string | URL) {
    requests.push(`xhr:${String(url)}`)
  })
  return { requests, restore: () => xhrOpen.mockRestore() }
}

async function renderAt(path: string) {
  window.history.replaceState(null, '', path)
  const utils = render(<App />)
  // Wait for the data to load (or fail) before asserting.
  await waitFor(() => expect(screen.queryByText(/Loading the latest published figures/)).not.toBeInTheDocument())
  return utils
}

/** Every element that would make the browser fetch something. */
function resourceUrls(): string[] {
  const urls: string[] = []
  document.querySelectorAll('[src], [srcset], [poster], [data], link[href]').forEach((el) => {
    for (const attr of ['src', 'srcset', 'poster', 'data']) {
      const v = el.getAttribute(attr)
      if (v) urls.push(v)
    }
    if (el.tagName === 'LINK') urls.push(el.getAttribute('href') ?? '')
  })
  return urls
}

const isThirdParty = (url: string) => /^(https?:)?\/\//i.test(url) && !url.startsWith(window.location.origin)

describe('every page', () => {
  it.each(PAGES)('$path renders its heading and the disclaimer', async ({ path, heading }) => {
    serve()
    await renderAt(path)
    expect(screen.getByRole('heading', { level: 1, name: heading })).toBeInTheDocument()
    const footer = screen.getByRole('contentinfo')
    expect(footer).toHaveTextContent(/paper trading/i)
    expect(footer).toHaveTextContent(/delayed/i)
    expect(footer).toHaveTextContent(/not investment advice/i)
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument()
  })

  it.each(PAGES)('$path requests nothing but its own data file', async ({ path }) => {
    const { requests, restore } = serve()
    await renderAt(path)
    restore()
    expect(requests).toEqual(['/data/portfolio.json'])
    expect(resourceUrls().filter(isThirdParty)).toEqual([])
    expect(document.querySelectorAll('iframe, script[src^="http"], img[src^="http"]')).toHaveLength(0)
  })

  it.each(PAGES)('$path names the current page in the navigation', async ({ path }) => {
    serve()
    await renderAt(path)
    const current = within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { current: 'page' })
    expect(current).toHaveAttribute('href', path)
  })
})

describe('overview', () => {
  it('states the account in a sentence and shows the headline figures', async () => {
    serve()
    await renderAt('/')
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(
      'Since Jan 5, 2026, this system has traded $100,000 of paper money. After the session of Jun 11, 2026, it was worth $102,907.77.',
    )
    const totals = screen.getByRole('region', { name: 'Headline figures' })
    const figure = (label: string) => within(totals).getByText(label).closest('div')!.querySelector('dd')!.textContent
    expect(figure('Total return')).toBe('+2.91%')
    expect(figure('Max drawdown')).toBe(`${MINUS}8.23%`)
    expect(figure('Win rate')).toBe('50%')
    expect(figure('Round trips')).toBe('20')
    expect(figure('Days live')).toBe('159')
    const note = screen.getByText(/Best session/)
    expect(note).toHaveTextContent(`Best session +1.61% on Feb 12, 2026; worst ${MINUS}2.21% on Apr 17, 2026.`)
    // A session the broker left out is never folded into the next day's change, and the page says so.
    expect(note).toHaveTextContent('Each session is measured against the one before it')
    expect(note).toHaveTextContent('a session missing from the broker’s history is skipped')
    expect(note).toHaveTextContent(`Last completed session ${MINUS}1.18%.`)
    expect(note).toHaveTextContent('no equity figure on this site moves during a session')
    // As the exporter computes them: from closing equity of completed sessions only.
    expect(note).toHaveTextContent(
      'Total return and drawdown are measured on the equity at the end of each session since the start, up to the last completed session',
    )
    expect(note).not.toHaveTextContent(/current equity|intraday|session in progress/i)
    expect(note).toHaveTextContent(`${fixture.account.daily.length} sessions on the equity curve.`)
  })

  it('carries the as-of time and the paper-trading disclaimer up top', async () => {
    serve()
    await renderAt('/')
    const main = screen.getByRole('main')
    expect(main).toHaveTextContent('As of Jun 12, 2026, 6:05 PM ET.')
    expect(main).toHaveTextContent(/paper trading: the account holds simulated money/i)
    expect(main).toHaveTextContent(/this is not investment advice/i)
  })

  it('states the real publishing cadence, not a 15-minute delay', async () => {
    serve()
    await renderAt('/')
    const main = screen.getByRole('main')
    expect(main).toHaveTextContent('exports its figures about every 15 minutes')
    expect(main).toHaveTextContent('about every 30 minutes during US market hours on weekdays and about every 6 hours')
    expect(main).toHaveTextContent('up to about 45 minutes during the session and about 6 hours outside it')
    expect(main).toHaveTextContent('Equity and returns are shown as of the end of the last completed session.')
    expect(main).not.toHaveTextContent(/lag the account by up to about 15 minutes/)
  })

  it('draws only the account’s own equity, with the attribution logo on', async () => {
    serve()
    await renderAt('/')
    await waitFor(() => expect(chartCalls.created).toBe(1))
    expect(chartCalls.options[0]).toMatchObject({ layout: { attributionLogo: true } })
    expect(chartCalls.series[0]!.kind).toEqual({ type: 'Baseline' })
    const points = chartCalls.data[0] as { time: string; value: number }[]
    expect(points).toHaveLength(fixture.account.daily.length)
    expect(points.at(-1)).toEqual({ time: '2026-06-11', value: 102907.77 })
    expect(chartCalls.priceLines[0]).toMatchObject({ price: 100000, title: 'Start' })
  })

  it('gives the chart a text alternative and a table of every point', async () => {
    serve()
    await renderAt('/')
    const chart = screen.getByRole('img', { name: /Daily closing equity from Jan 5, 2026 \(\$100,000\.00\)/ })
    expect(chart).toBeInTheDocument()
    const user = userEvent.setup()
    await user.click(screen.getByText('Equity by day, as a table'))
    const table = screen.getByRole('table', { name: /Closing equity for each session/ })
    expect(within(table).getAllByRole('row')).toHaveLength(fixture.account.daily.length + 1)
  })

  it('draws daily equity only: one point per session and no intraday view', async () => {
    serve()
    await renderAt('/')
    await waitFor(() => expect(chartCalls.created).toBe(1))
    expect(screen.queryByRole('button', { name: 'Latest session' })).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Chart range' })).not.toBeInTheDocument()
    expect('intraday' in fixture.account).toBe(false)
    const points = chartCalls.data[0] as { time: string }[]
    expect(points.map((p) => p.time)).toEqual(fixture.account.daily.map((d) => d.date))
    expect(new Set(points.map((p) => p.time)).size).toBe(points.length)
    expect(chartCalls.options[0]).toMatchObject({ timeScale: { timeVisible: false } })
    expect(screen.queryByText(/through the session/)).not.toBeInTheDocument()
  })

  it('lists open positions with the own stop level, and nothing worked out from a market price', async () => {
    serve()
    await renderAt('/')
    const table = screen.getByRole('table', { name: /Entry is the account’s own average fill price/ })
    const headers = within(table)
      .getAllByRole('columnheader')
      .map((h) => h.textContent)
    expect(headers).toEqual(['Symbol', 'Quantity', 'Entry', 'Stop'])
    // One position from the signals: the fixture's put is still open, so nothing was assigned.
    expect(within(table).getAllByRole('row')).toHaveLength(2)
    const nova = within(table).getByRole('rowheader', { name: 'NOVA' }).closest('tr')!
    // entry 250.50 less the 2% stop
    expect([...nova.querySelectorAll('td')].map((c) => c.textContent)).toEqual(['40', '250.50', '245.492% under'])
    expect(table).toHaveAccessibleName(/a check, not an order at the broker/)
    expect(table).toHaveAccessibleName(/a gap or a fast bar can take the sale well below it/)
    expect(table).toHaveAccessibleName(/the entry of its strategy’s simulated trade, the close of the bar the buy signal fired on/)
    expect(table.querySelector('thead')).not.toHaveTextContent(/unrealized|return|distance/i)
    expect(screen.getByText(/Short 1 VOLT put/)).toBeInTheDocument()
  })

  it('says a dash is a stop the site cannot vouch for, and a level is not a promise', async () => {
    const doc = structuredClone(fixture)
    // The put was assigned: its shares sit beside the engine's own position, with no stop.
    const assigned = { symbol: 'VOLT', side: 'long', qty: 100, avg_entry_price: 35, stop_pct: null }
    serve({ ...doc, positions: { equities: [...doc.positions.equities, assigned], options: [] } })
    await renderAt('/')
    const table = screen.getByRole('table', { name: /Entry is the account’s own average fill price/ })
    const volt = within(table).getByRole('rowheader', { name: 'VOLT' }).closest('tr')!
    expect([...volt.querySelectorAll('td')].map((c) => c.textContent)).toEqual(['100', '35.00', '–'])
    expect(within(table).getByRole('rowheader', { name: 'NOVA' }).closest('tr')).toHaveTextContent('245.49')
    expect(table).toHaveAccessibleName(/A dash means the site cannot vouch for a stop/)
    // The engine sells whatever is held when a strategy's simulated stop fires, opener or not.
    expect(table).toHaveAccessibleName(/a strategy that is long in its own simulation can still stop it out/)
    expect(table).toHaveAccessibleName(/A level shown is not a promise either/)
    expect(table).not.toHaveAccessibleName(/holds no stop on the position/)
  })

  it('shows the research board with bias, priority and size but no notes', async () => {
    serve()
    await renderAt('/')
    const board = screen.getByRole('table', { name: /Board for the session of Jun 12, 2026/ })
    const rows = within(board).getAllByRole('row').slice(1)
    expect(rows.map((r) => within(r).getByRole('rowheader').textContent).slice(0, 3)).toEqual(['ACME', 'NOVA', 'LUMA'])
    expect(rows[0]).toHaveTextContent('Long')
    expect(rows[0]).toHaveTextContent('97%')
    // The size is of the share book, which the options sleeve’s reserve has come off.
    const legend = screen.getByText(/Among longs, the higher priority wins/)
    expect(legend).toHaveTextContent('equity less the 20% the options sleeve keeps back')
    expect(legend).toHaveTextContent('A size of 97% is about 78% of equity.')
  })

  it('calls the board’s size a share of equity when no sleeve keeps anything back', async () => {
    const doc = structuredClone(fixture)
    doc.strategy.risk.sleeve_reserve_pct = 0
    serve(doc)
    await renderAt('/')
    expect(screen.getByText(/Among longs, the higher priority wins/)).toHaveTextContent(
      'Size is the share of equity a buy would use.',
    )
  })

  it('shows a live standing order and hides it once expired', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-06-13T00:00:00Z'))
    serve()
    await renderAt('/')
    expect(screen.getByText(/Buy allowed on a close above/)).toHaveTextContent('61.50')
    expect(screen.queryByText(/have not been updated since/)).not.toBeInTheDocument()
  })

  it('says so when the figures are old', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-07-01T00:00:00Z'))
    serve()
    await renderAt('/')
    expect(screen.getByText(/have not been updated since/)).toBeInTheDocument()
    expect(screen.queryByText(/Buy allowed on a close/)).not.toBeInTheDocument()
  })

  it.each([
    ['25 hours', '2026-06-13T23:05:00Z', false],
    ['27 hours', '2026-06-14T01:05:00Z', true],
    // as_of is a Friday evening: by Monday morning a healthy pipeline has
    // published many times over the weekend, so silence that long is a fault.
    ['the weekend', '2026-06-15T13:00:00Z', true],
  ])('flags figures %s old on every page, not only the overview', async (_age, now, stale) => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(now))
    for (const path of ['/', '/trades', '/strategy', '/recaps']) {
      serve()
      const { unmount } = await renderAt(path)
      const notice = screen.queryByText(/have not been updated since/)
      if (stale) {
        expect(notice, path).toBeInTheDocument()
        expect(notice!.closest('.notice')).toHaveTextContent('Jun 12, 2026, 6:05 PM ET')
        expect(notice!.closest('.notice')).toHaveTextContent('refreshes at least every 6 hours')
      } else expect(notice, path).not.toBeInTheDocument()
      unmount()
    }
  })

  it('says the account is in cash when nothing is held', async () => {
    serve({ ...structuredClone(fixture), positions: { equities: [], options: [] } })
    await renderAt('/')
    expect(screen.getByText(/None\. The account is in cash/)).toBeInTheDocument()
  })

  it('names sections that did not report', async () => {
    const doc = structuredClone(fixture) as Record<string, unknown>
    doc.account = null
    doc.signals = null
    ;(doc.meta as Record<string, unknown>).unavailable = ['account', 'signals']
    serve(doc)
    await renderAt('/')
    expect(screen.getByText('Not reported in this export: account, research board.')).toBeInTheDocument()
    expect(screen.getByText(/The account did not report in the latest export/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/keeping its own books/)
  })
})

describe('trades', () => {
  it('summarises the closed trades in a ledger', async () => {
    serve()
    await renderAt('/trades')
    const total = screen.getByText('Realized profit and loss').closest('div')!
    expect(total).toHaveTextContent('+$319.61')
    expect(screen.getByText('Average loss').closest('div')).toHaveTextContent(`${MINUS}$164.22 (${MINUS}1.98%)`)
    expect(screen.getByText('Profit factor').closest('div')).toHaveTextContent('1.19')
    expect(screen.getByText(/Profit factor is gross profit/)).toHaveTextContent(
      'a fee charged in dollars on the sale is not deducted',
    )
  })

  it('puts Return and P&L straight after the symbol, where a phone can see them', async () => {
    serve()
    await renderAt('/trades')
    const table = screen.getByRole('table', { name: /closed round trips at the account’s own fill prices/ })
    const headers = within(table)
      .getAllByRole('columnheader')
      .map((h) => h.textContent)
    expect(headers).toEqual(['Symbol', 'Return', 'P&L', 'Opened', 'Closed', 'Held', 'Quantity', 'Entry', 'Exit'])
    const foot = table.querySelector('tfoot tr')!
    expect(foot.querySelector('th')).toHaveAttribute('colspan', '2')
    const cells = [...foot.querySelectorAll('td')]
    const total = fixture.trades.round_trips.reduce((sum, t) => sum + t.pnl, 0)
    expect(cells[0]).toHaveTextContent(`+$${total.toFixed(2)}`)
    // header, rows and footer all span the same number of columns
    const width = (tr: Element) => [...tr.children].reduce((n, c) => n + Number(c.getAttribute('colspan') ?? 1), 0)
    expect(new Set([...table.querySelectorAll('tr')].map(width))).toEqual(new Set([headers.length]))
  })

  it('sorts the round trips from the column headings', async () => {
    serve()
    await renderAt('/trades')
    const user = userEvent.setup()
    const table = screen.getByRole('table', { name: /closed round trips at the account’s own fill prices/ })
    const firstSymbol = () => within(table).getAllByRole('row')[1]!.querySelector('th')!.textContent
    expect(firstSymbol()).toBe('NOVA')
    expect(within(table).getByRole('columnheader', { name: /Closed/ })).toHaveAttribute('aria-sort', 'descending')

    await user.click(within(table).getByRole('button', { name: 'P&L' }))
    const best = [...fixture.trades.round_trips].sort((a, b) => b.pnl - a.pnl)[0]!
    expect(firstSymbol()).toBe(best.symbol)
    expect(within(table).getByRole('columnheader', { name: /P&L/ })).toHaveAttribute('aria-sort', 'descending')

    await user.click(within(table).getByRole('button', { name: 'P&L' }))
    expect(within(table).getByRole('columnheader', { name: /P&L/ })).toHaveAttribute('aria-sort', 'ascending')
    const worst = [...fixture.trades.round_trips].sort((a, b) => a.pnl - b.pnl)[0]!
    expect(firstSymbol()).toBe(worst.symbol)
  })

  it('labels quarter-percent return bins exactly', async () => {
    const doc = structuredClone(fixture)
    const returns = [-0.006, -0.004, -0.001, 0.002, 0.003, 0.007]
    doc.trades.round_trips = doc.trades.round_trips.slice(0, returns.length).map((t, i) => ({ ...t, pnl_pct: returns[i]! }))
    serve(doc)
    await renderAt('/trades')
    await userEvent.setup().click(screen.getByText('The same counts, as a table'))
    const table = screen.getByRole('table', { name: /Round trips by return range/ })
    const rows = within(table)
      .getAllByRole('row')
      .slice(1)
      .map((r) => [...r.querySelectorAll('td')].map((c) => c.textContent))
    expect(rows).toEqual([
      [`${MINUS}0.75%`, `${MINUS}0.5%`, '1'],
      [`${MINUS}0.5%`, `${MINUS}0.25%`, '1'],
      [`${MINUS}0.25%`, '0%', '1'],
      ['0%', '+0.25%', '1'],
      ['+0.25%', '+0.5%', '1'],
      ['+0.5%', '+0.75%', '1'],
    ])
  })

  it('draws the return distribution with a text alternative', async () => {
    serve()
    await renderAt('/trades')
    const chart = screen.getByRole('img', { name: /Histogram of 20 round-trip returns: 10 above zero, 10 below/ })
    expect(chart.querySelectorAll('rect').length).toBeGreaterThan(3)
  })
})

describe('strategy & risk', () => {
  it('shows each symbol’s parameters and the risk limits from the data', async () => {
    serve()
    await renderAt('/strategy')
    const table = screen.getByRole('table', { name: /The engine’s settings for each symbol/ })
    const acme = within(table).getByRole('rowheader', { name: 'ACME' }).closest('tr')!
    expect(acme).toHaveTextContent('MACD + SMA trend filter (fast 12, slow 26, signal 9, trend 150)')
    expect(acme).toHaveTextContent('Bollinger + RSI (RSI 6, band 20, width 1.5)')
    expect(within(table).getByRole('rowheader', { name: 'QBIT/USD' }).closest('tr')).toHaveTextContent('Paused')
    expect(screen.getByText('Daily loss halt').closest('div')).toHaveTextContent('Buys stop at 5% below the previous close')
    expect(screen.getByText('Stop on each position').closest('div')).toHaveTextContent('2% to 3% below entry')
    expect(within(table).getByRole('rowheader', { name: 'ORBX' }).closest('tr')).toHaveTextContent(
      'EMA cross (fast 12, slow 50, window 200)',
    )
    // Explains the strategies in use, in plain words; none the engine no longer runs.
    expect(screen.getByText('EMA cross')).toBeInTheDocument()
    expect(screen.queryByText(/Donchian/)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'How a trade happens' })).toBeInTheDocument()
  })

  it('explains only the strategies some symbol runs', async () => {
    const doc = structuredClone(fixture)
    const symbols = (doc.strategy.symbols as { strategies: { type: string }[] }[]).map((s) => ({
      ...s,
      strategies: s.strategies.filter((x) => x.type !== 'ema'),
    }))
    serve({ ...doc, strategy: { ...doc.strategy, symbols } })
    await renderAt('/strategy')
    expect(screen.getByText('Bollinger + RSI')).toBeInTheDocument()
    expect(screen.queryByText('EMA cross')).not.toBeInTheDocument()
  })

  it('describes the risk gate as relay/risk_filter.py applies it', async () => {
    serve()
    await renderAt('/strategy')
    const main = screen.getByRole('main')
    // No latch: the halt is checked before every buy and lifts when equity recovers.
    expect(main).not.toHaveTextContent(/for the rest of the day/)
    expect(main).toHaveTextContent('once equity recovers above it buys go out again')
    // Nothing enforces a research-set position count: one at a time is fixed in code,
    // and shares the put sleeve was assigned are held outside that rotation.
    expect(screen.getByText('Positions at once').closest('div')).toHaveTextContent(
      'One from the signals, fixed in code, plus any shares the options sleeve was assigned',
    )
    expect(main).toHaveTextContent('the rotation never sells them, so they can be held alongside the engine’s position')
    expect(main).toHaveTextContent('If a put is assigned, the shares are held alongside the share book’s own position')
    expect(main).not.toHaveTextContent(/at most 10 positions|\b10 positions/)
    expect(screen.getByText('Largest buy order').closest('div')).toHaveTextContent('10,000 shares')
    expect(screen.getByText('Share buys outside regular hours').closest('div')).toHaveTextContent('Allowed')
    expect(main).toHaveTextContent('A share sale may go out at any time in that wider window whatever research says')
    // A share with no bias declared is not refused: only flat or short blocks a buy.
    expect(main).not.toHaveTextContent(/A share also needs a long bias/)
    expect(main).toHaveTextContent('when research has marked the symbol flat or short')
  })

  it('says a stop is checked at the bar close, not a cap on the loss, with the worst trip so far', async () => {
    serve()
    await renderAt('/strategy')
    const main = screen.getByRole('main')
    expect(main).toHaveTextContent('so a stop is not a cap on the loss')
    const worst = Math.min(...fixture.trades.round_trips.map((t) => t.pnl_pct))
    expect(main).toHaveTextContent(`The worst closed round trip so far returned ${signedPct(worst)}.`)
    expect(main).toHaveTextContent('after a gap or a fast bar the sale can land well past it')
    // The stop follows the strategy's simulated trade, not the fill, and can be lost with it.
    expect(main).toHaveTextContent('Each strategy keeps its own simulated trade, entered at the close of the bar its buy signal fired on')
    expect(main).toHaveTextContent('the position that buy opens can be left with no stop at all')
  })

  it('reads a missing extended-hours permission as not allowed, as the gate does', async () => {
    const doc = structuredClone(fixture)
    doc.strategy.risk.allow_extended_hours = false
    serve(doc)
    await renderAt('/strategy')
    expect(screen.getByText('Share buys outside regular hours').closest('div')).toHaveTextContent('Not allowed')
  })

  it('sizes a buy off equity less the options sleeve’s reserve', async () => {
    serve()
    await renderAt('/strategy')
    const table = screen.getByRole('table', { name: /The engine’s settings for each symbol/ })
    expect(table).toHaveAccessibleName(/The share book is equity less the 20% the options sleeve keeps back, so 97% is about 78% of equity/)
    const acme = within(table).getByRole('rowheader', { name: 'ACME' }).closest('tr')!
    expect(acme.lastElementChild).toHaveTextContent('97%78% of equity')
    expect(screen.getByText('Kept back for the options sleeve').closest('div')).toHaveTextContent('20% of equity')
  })

  it('marks a symbol off research’s watchlist, crypto included, and says crypto still needs the watchlist', async () => {
    const doc = structuredClone(fixture)
    for (const s of doc.strategy.symbols) if (s.symbol === 'QBIT/USD') Object.assign(s, { enabled: true, on_watchlist: false })
    serve(doc)
    await renderAt('/strategy')
    const table = screen.getByRole('table', { name: /The engine’s settings for each symbol/ })
    expect(within(table).getByRole('rowheader', { name: 'QBIT/USD' }).closest('tr')).toHaveTextContent(
      'Not on research’s watchlist',
    )
    expect(within(table).getByRole('rowheader', { name: 'ACME' }).closest('tr')).toHaveTextContent('Trading')
    expect(screen.getByText(/so it is exempt from the bias, though not from the watchlist/)).toBeInTheDocument()
    expect(screen.queryByText(/judged on its strategy signal alone/)).not.toBeInTheDocument()
  })

  it('writes the per-symbol settings out as a list for phones', async () => {
    serve()
    await renderAt('/strategy')
    const narrow = screen.getByTestId('symbols-narrow')
    expect(narrow).toHaveClass('sm:hidden')
    expect(screen.getByRole('table', { name: /The engine’s settings for each symbol/ }).closest('.max-sm\\:hidden')).not.toBeNull()
    const list = within(narrow).getByRole('list', { name: 'Per-symbol settings' })
    const entries = within(list).getAllByRole('listitem').filter((li) => li.parentElement === list)
    expect(entries.map((li) => li.querySelector('strong')!.textContent)).toEqual(
      fixture.strategy.symbols.map((s) => s.symbol).sort((a, b) => {
        const en = (x: string) => fixture.strategy.symbols.find((s) => s.symbol === x)!.enabled
        return Number(en(b)) - Number(en(a)) || a.localeCompare(b)
      }),
    )
    const acme = entries.find((li) => li.querySelector('strong')!.textContent === 'ACME')!
    expect(acme).toHaveTextContent('MACD + SMA trend filter (fast 12, slow 26, signal 9, trend 150)')
    expect(acme).toHaveTextContent('Deploy97% (78% of equity)')
  })
})

describe('how it’s built', () => {
  it('draws the architecture with a text alternative and links the repository', async () => {
    serve()
    await renderAt('/how-its-built')
    expect(screen.getByRole('img', { name: /How the system fits together/ })).toBeInTheDocument()
    const repo = screen.getByRole('link', { name: 'github.com/zhen-qian101/alpaca-trading' })
    expect(repo).toHaveAttribute('href', 'https://github.com/zhen-qian101/alpaca-trading')
  })

  it('draws research’s summary into the recap renderer, beside the journal', async () => {
    serve()
    await renderAt('/how-its-built')
    const svg = screen.getByRole('img', { name: /How the system fits together/ })
    const groups = Array.from(svg.querySelectorAll('g'))
    const box = (title: string) => {
      const rect = groups.find((g) => g.querySelector('text.title')?.textContent === title)!.querySelector('rect')!
      const n = (a: string) => Number(rect.getAttribute(a))
      return { x: n('x'), y: n('y'), w: n('width'), h: n('height') }
    }
    // The first and last points of an edge drawn with M, H and V only.
    const ends = (label: string) => {
      const d = groups.find((g) => g.querySelector('text.edge-label')?.textContent === label)?.querySelector('path')
      expect(d, `an edge labelled “${label}”`).toBeTruthy()
      let x = 0
      let y = 0
      const points: [number, number][] = []
      for (const [, cmd, a, b] of d!.getAttribute('d')!.matchAll(/([MHV])\s*(-?[\d.]+)(?:\s+(-?[\d.]+))?/g)) {
        if (cmd === 'M') [x, y] = [Number(a), Number(b)]
        else if (cmd === 'H') x = Number(a)
        else y = Number(a)
        points.push([x, y])
      }
      return { from: points[0]!, to: points[points.length - 1]! }
    }
    const touches = ([px, py]: [number, number], b: ReturnType<typeof box>) =>
      px >= b.x && px <= b.x + b.w && py >= b.y && py <= b.y + b.h
    const summary = ends('summary')
    expect(touches(summary.from, box('Claude research'))).toBe(true)
    expect(touches(summary.to, box('Recap renderer'))).toBe(true)
    const journal = ends('journal')
    expect(touches(journal.from, box('Relay'))).toBe(true)
    expect(touches(journal.to, box('Recap renderer'))).toBe(true)
    expect(svg.querySelector('desc')).toHaveTextContent('research’s summary of the day')
    expect(Array.from(svg.querySelectorAll('text.edge-label'), (t) => t.textContent)).not.toContain('trade journal')
  })

  it('claims only what this site does, not that the home computer is unreachable', async () => {
    serve()
    await renderAt('/how-its-built')
    const main = screen.getByRole('main')
    expect(main).not.toHaveTextContent(/nothing on the internet can reach/i)
    expect(main).not.toHaveTextContent(/internet sees only/i)
    expect(main).not.toHaveTextContent(/speech and video models run in ComfyUI/i)
    expect(main).toHaveTextContent('This site has no server code and never contacts the home computer.')
  })

  it('says which price the published equity still gives back: an end-of-session one, never a live one', async () => {
    serve()
    await renderAt('/how-its-built')
    const leftOut = screen.getByRole('region', { name: 'What this site leaves out, and why' })
    expect(leftOut).toHaveTextContent('Equity during a session.')
    expect(leftOut).toHaveTextContent('equity, the returns and the curve stop at the last completed session')
    expect(leftOut).toHaveTextContent('price at the end of that session can be worked out, but never a live one')
    expect(leftOut).not.toHaveTextContent(/Equity through the day/)
  })

  it('scopes “no market data, no model text” to the site’s own figures, and says what the videos show', async () => {
    serve()
    await renderAt('/how-its-built')
    const leftOut = screen.getByRole('region', { name: 'What this site leaves out, and why' })
    expect(leftOut).toHaveTextContent('Market data, from the site’s own figures and charts.')
    expect(leftOut).toHaveTextContent(
      'nothing in the figures, tables or charts on these pages comes from a market-data feed',
    )
    expect(leftOut).toHaveTextContent(
      'The recap videos are not covered by this: most of them show the live trading dashboard, including market candles and current prices.',
    )
    expect(leftOut).toHaveTextContent('Model-written text.')
    expect(leftOut).toHaveTextContent('a language model writes their scripts and YouTube titles')
    // The old, unscoped claims.
    expect(leftOut).not.toHaveTextContent(/the model’s own words are not published/)
    expect(leftOut).not.toHaveTextContent(/do not allow republishing/)
    const main = screen.getByRole('main')
    expect(main).toHaveTextContent('A language model turns that summary and the account’s own figures into a script and a title.')
    expect(main).toHaveTextContent('One slide is usually a screenshot of the live dashboard, market candles and current prices included.')
    expect(main).not.toHaveTextContent(/reads the day’s journal/)
  })
})

describe('recaps', () => {
  it('loads nothing from YouTube until play is pressed', async () => {
    const { requests, restore } = serve()
    await renderAt('/recaps')
    expect(within(screen.getByRole('main')).getAllByRole('listitem')).toHaveLength(4)
    expect(document.querySelector('iframe')).toBeNull()
    expect(resourceUrls().filter(isThirdParty)).toEqual([])

    await userEvent.setup().click(screen.getByRole('button', { name: /^Play the recap for Jun 12, 2026, 1 minute 5 seconds\./ }))
    const frames = document.querySelectorAll('iframe')
    expect(frames).toHaveLength(1)
    const src = new URL(frames[0]!.getAttribute('src')!)
    expect(src.origin).toBe('https://www.youtube-nocookie.com')
    expect(src.pathname).toBe('/embed/FAKEyt00004')
    expect(src.searchParams.get('autoplay')).toBe('1')
    expect(frames[0]).toHaveAttribute('title', 'Recap for Jun 12, 2026')
    // The only third-party resource on the page is that one player.
    expect(resourceUrls().filter(isThirdParty)).toEqual([frames[0]!.getAttribute('src')])
    restore()
    expect(requests).toEqual(['/data/portfolio.json'])
  })

  it('moves keyboard focus into the player that replaces the play button', async () => {
    serve()
    await renderAt('/recaps')
    const user = userEvent.setup()
    const play = screen.getByRole('button', { name: /^Play the recap for Jun 11, 2026/ })
    play.focus()
    await user.keyboard('{Enter}')
    const frame = document.querySelector('iframe')!
    expect(frame).toHaveAttribute('title', 'Recap for Jun 11, 2026')
    expect(frame).toHaveFocus()
    expect(document.activeElement).not.toBe(document.body)
  })
})

describe('recap labels and what the videos show', () => {
  it('heads each recap with its date', async () => {
    serve()
    await renderAt('/recaps')
    const headings = within(screen.getByRole('main'))
      .getAllByRole('heading', { level: 2 })
      .map((h) => h.textContent)
    expect(headings).toEqual(['Recap for Jun 12, 2026', 'Recap for Jun 11, 2026', 'Recap for Jun 10, 2026', 'Recap for Jun 9, 2026'])
  })

  it('shows no model-written title, even from a file that carries one', async () => {
    // An older exporter published the model's headline; the check refuses that file.
    const doc = structuredClone(fixture) as unknown as { recaps: Record<string, unknown>[] }
    doc.recaps[0]!.title = 'A model wrote this headline'
    serve(doc)
    await renderAt('/recaps')
    expect(screen.queryByText(/A model wrote this headline/)).not.toBeInTheDocument()
    expect(screen.getByText(/The published figures failed this page’s checks/)).toBeInTheDocument()
  })

  it('says the videos are AI-made and show the live dashboard, and scopes the site’s own promise', async () => {
    serve()
    await renderAt('/recaps')
    const main = screen.getByRole('main')
    expect(main).toHaveTextContent('A language model writes each script and YouTube title')
    expect(main).toHaveTextContent('On weekdays an AI-generated presenter reads the script')
    expect(main).toHaveTextContent(
      'Most of the videos also show the live trading dashboard, including market candles and current prices from the market-data feeds.',
    )
    expect(main).toHaveTextContent(
      'its own figures, tables and charts contain no third-party market data and no model-written text',
    )
    expect(main).not.toHaveTextContent(/from the journal/)
  })
})

describe('missing or bad data', () => {
  it('explains that figures are not published and still renders the pages', async () => {
    serve('not found', { status: 404 })
    await renderAt('/strategy')
    expect(screen.getByText('Account figures are not published right now.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'How a trade happens' })).toBeInTheDocument()
  })

  it('shows none of a file that fails the check', async () => {
    serve({ ...structuredClone(fixture), research_notes: 'a model wrote this' })
    await renderAt('/')
    expect(screen.getByText(/failed this page’s checks/)).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('102,907.77')
    expect(document.body).not.toHaveTextContent('a model wrote this')
  })
})

describe('wide tables', () => {
  it('fade at the right edge while columns are hidden there, and stop once scrolled to the end', async () => {
    serve()
    await renderAt('/trades')
    const region = screen.getByRole('region', { name: 'Round trips' })
    const frame = region.parentElement!
    expect(frame).toHaveClass('table-frame')
    expect(frame).not.toHaveAttribute('data-more')
    // jsdom lays nothing out: give the region a phone's width and a wider table.
    let scrollLeft = 0
    Object.defineProperties(region, {
      scrollWidth: { configurable: true, get: () => 900 },
      clientWidth: { configurable: true, get: () => 390 },
      scrollLeft: { configurable: true, get: () => scrollLeft },
    })
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(frame).toHaveAttribute('data-more', 'true')
    scrollLeft = 510
    act(() => {
      region.dispatchEvent(new Event('scroll'))
    })
    expect(frame).not.toHaveAttribute('data-more')
  })

  it('do not fade when everything fits', async () => {
    serve()
    await renderAt('/trades')
    expect(screen.getByRole('region', { name: 'Round trips' }).parentElement).not.toHaveAttribute('data-more')
  })
})

describe('navigation and preferences', () => {
  it('moves between pages without a reload and focuses the new heading', async () => {
    serve()
    await renderAt('/')
    const user = userEvent.setup()
    await user.click(within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'Trades' }))
    const heading = screen.getByRole('heading', { level: 1, name: 'Trades' })
    expect(heading).toHaveFocus()
    expect(window.location.pathname).toBe('/trades')
    expect(document.title).toBe('Trades | Alpaca Trading System')

    act(() => {
      window.history.pushState(null, '', '/recaps')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    expect(screen.getByRole('heading', { level: 1, name: 'Recaps' })).toBeInTheDocument()
  })

  it('shows a not-found page for an unknown address', async () => {
    serve()
    await renderAt('/no-such-page')
    expect(screen.getByRole('heading', { level: 1, name: 'Page not found' })).toBeInTheDocument()
  })

  it('switches theme and remembers the choice', async () => {
    serve()
    await renderAt('/')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Use dark theme' }))
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark')
    expect(screen.getByRole('button', { name: 'Use light theme' })).toBeInTheDocument()
  })

  it('offers a skip link to the main content', async () => {
    serve()
    await renderAt('/')
    expect(screen.getByRole('link', { name: 'Skip to content' })).toHaveAttribute('href', '#main')
  })
})
