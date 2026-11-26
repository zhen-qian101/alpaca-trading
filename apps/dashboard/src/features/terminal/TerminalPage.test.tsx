import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SymbolChart } from '../../contract.ts'
import { queryKeys } from '../../lib/queries.ts'
import { FIXTURE_AS_OF, optionsSleeveFixture } from '../../test/fixtures.ts'
import { mockApi, snapshotWith } from '../../test/mockApi.ts'
import { freezeTime, renderApp } from '../../test/render.tsx'

// lightweight-charts needs a real canvas. The chart's drawing logic is covered
// by chartMath tests; here a recorder stands in for it.
const chartCalls: unknown[][] = []
vi.mock('./chart/signalChart.ts', () => ({
  SignalChart: class {
    constructor() {
      chartCalls.push(['create'])
    }
    setData(symbol: string, data: SymbolChart) {
      chartCalls.push(['setData', symbol, data.candles.length])
    }
    setZone(zone: string) {
      chartCalls.push(['setZone', zone])
    }
    setPalette() {}
    destroy() {
      chartCalls.push(['destroy'])
    }
  },
  readPalette: () => ({}),
}))

beforeEach(() => {
  chartCalls.length = 0
  freezeTime(new Date(Date.parse(FIXTURE_AS_OF) + 30_000).toISOString())
})

describe('terminal', () => {
  it('shows the account hero, balances and the loss halt', async () => {
    mockApi()
    renderApp('/')
    expect(await screen.findByText('$102,161.29')).toBeInTheDocument()
    expect(screen.getByText('+$1,685.38 (+1.68%) today')).toBeInTheDocument()
    const balances = screen.getByText('Cash').closest('dl')!
    expect(within(balances).getByText('$3,377.02')).toBeInTheDocument()
    expect(within(balances).getByText('$6,754.04')).toBeInTheDocument()
    expect(within(balances).getByText('$100,475.91')).toBeInTheDocument()
    expect(screen.getByRole('meter', { name: "Share of today's loss limit used" })).toHaveAttribute('aria-valuenow', '0')
    expect(screen.getByText('Halts buys only — exits always allowed.')).toBeInTheDocument()
    expect(screen.getByText('Paper')).toBeInTheDocument()
  })

  it('flags a tripped halt in words', async () => {
    const snap = snapshotWith()
    if (!('error' in snap.portfolio)) snap.portfolio.halt = { ...snap.portfolio.halt, tripped: true, used_frac: 1 }
    mockApi({ '/api/snapshot': { status: 200, body: snap } })
    renderApp('/')
    expect(await screen.findByText('Halted: new buys blocked')).toBeInTheDocument()
  })

  // F7: equity and the loss line change with nearly every snapshot; as live
  // regions they were read out about once a minute. Only state changes are.
  it('keeps moving figures out of live regions and announces the halt tripping', async () => {
    mockApi()
    const { client } = renderApp('/')
    const equity = await screen.findByText('$102,161.29')
    expect(equity.closest('[aria-live]')).toBeNull()
    expect(screen.getByText(/lost of the/).closest('[aria-live]')).toBeNull()
    const halt = within(screen.getByRole('region', { name: 'Account' })).getByRole('status')
    expect(halt).toHaveTextContent('') // the state the page loaded with is on screen, not read out

    const moved = snapshotWith()
    if (!('error' in moved.portfolio)) moved.portfolio = { ...moved.portfolio, equity: 102_000.01 }
    act(() => client.setQueryData(queryKeys.snapshot, moved))
    expect(await screen.findByText('$102,000.01')).toBeInTheDocument()
    expect(halt).toHaveTextContent('') // a new figure is not an announcement

    const tripped = snapshotWith()
    if (!('error' in tripped.portfolio)) tripped.portfolio.halt = { ...tripped.portfolio.halt, tripped: true, used_frac: 1 }
    act(() => client.setQueryData(queryKeys.snapshot, tripped))
    await waitFor(() => expect(halt).toHaveTextContent('Daily loss halt tripped: new buys are blocked.'))
  })

  it('lists share positions and the options sleeve short put with their room to the line', async () => {
    mockApi({ '/api/snapshot': { status: 200, body: snapshotWith({ options: optionsSleeveFixture }) } })
    renderApp('/')
    const table = await screen.findByRole('table', { name: /Open share positions and the options sleeve/ })
    const acme = within(table).getByRole('rowheader', { name: /ACME/ }).closest('tr')!
    expect(within(acme).getByText('325')).toBeInTheDocument()
    expect(within(acme).getByText('292.35')).toBeInTheDocument()
    expect(within(acme).getByText('+3.97%')).toBeInTheDocument()

    expect(within(table).getByRole('rowheader', { name: /Options sleeve: cash-secured puts on ORBT/ })).toBeInTheDocument()
    const put = within(table).getByRole('rowheader', { name: /ORBT 20 Mar 26 40 put/ }).closest('tr')!
    expect(within(put).getByText('short put')).toBeInTheDocument()
    expect(within(put).getByText('−1')).toBeInTheDocument()
    expect(within(put).getByText('40.00')).toBeInTheDocument()
    expect(within(put).getByText('+12.13%')).toBeInTheDocument()
    expect(within(put).getByText('+$73.00')).toBeInTheDocument()
    expect(within(table).getByText('live')).toBeInTheDocument()
  })

  it('says so when the snapshot carries no options sleeve', async () => {
    const snap = snapshotWith()
    delete snap.options
    mockApi({ '/api/snapshot': { status: 200, body: snap } })
    renderApp('/')
    expect(await screen.findByText(/This report carries no options data/)).toBeInTheDocument()
  })

  it('warns when assigned shares have nobody managing them', async () => {
    const options = {
      ...optionsSleeveFixture,
      short_put: null,
      assigned: {
        symbol: 'ORBT', qty: 100, avg_price: 40, unrealized: -120, unrealized_pct: -0.03,
        managed_by_engine: false, adopted: false, basis: 40, anchor: 38.8, sell_by: '2026-03-06',
      },
    }
    mockApi({ '/api/snapshot': { status: 200, body: snapshotWith({ options }) } })
    renderApp('/')
    expect(await screen.findByText(/Nothing on the home box is managing these shares/)).toBeInTheDocument()
    expect(screen.getByText('No put is open.')).toBeInTheDocument()
  })

  it('writes every contingency out on the rotation board', async () => {
    mockApi()
    renderApp('/')
    const board = await screen.findByRole('table', { name: /Rotation candidates/ })
    expect(within(board).getAllByRole('row')).toHaveLength(1 + 4 + 1) // header, 4 symbols, NOVA's contingency
    expect(within(board).getByText('57.50')).toBeInTheDocument()
    expect(within(board).getByText(/closes above/)).toBeInTheDocument()
    expect(within(board).getByText(/A close back above the prior range high/)).toBeInTheDocument()
    expect(within(board).getByText(/Fri 6 Mar 2026 09:30 ET/)).toBeInTheDocument()
    expect(within(board).getByText('held')).toBeInTheDocument()
  })

  // F8: at 360 px the table is wider than its scroll region and the
  // contingency cell spans four of its columns, so every line was cut off.
  // On a phone the table's detail rows give way to a full-width list.
  it('writes the contingencies out under the table on a phone', async () => {
    mockApi()
    renderApp('/')
    const board = await screen.findByRole('table', { name: /Rotation candidates/ })
    const detail = within(board).getByText(/A close back above the prior range high/).closest('tr')!
    expect(detail).toHaveClass('max-sm:hidden')
    const narrow = screen.getByTestId('contingencies-narrow')
    expect(narrow).toHaveClass('sm:hidden') // never both: one copy per width
    expect(board.contains(narrow)).toBe(false)
    expect(within(narrow).getByText('NOVA')).toBeInTheDocument()
    expect(within(narrow).getByText(/A close back above the prior range high/)).toBeInTheDocument()
    expect(within(narrow).getByText(/Fri 6 Mar 2026 09:30 ET/)).toBeInTheDocument()
  })

  it('shows engine liveness, lag and the log tail', async () => {
    mockApi()
    renderApp('/')
    expect(await screen.findByText('Engine live')).toBeInTheDocument()
    expect(screen.getByText('60 min behind')).toBeInTheDocument()
    expect(screen.getByText('NOVA', { selector: '.warn .mono' })).toBeInTheDocument()
    expect(screen.getByText(/Engine log, last 5 lines/)).toBeInTheDocument()
    expect(screen.getByText(/poll done in 2.1s; 0 orders/)).toBeInTheDocument()
  })

  // EXTRA A: SOXL, disabled in config since August, read "2158 h behind" in
  // warning colours. A symbol the engine no longer polls is neutral.
  it('shows a symbol the engine no longer polls as disabled, not as lag', async () => {
    mockApi()
    renderApp('/')
    await screen.findByText('Engine live')
    const engine = screen.getByRole('region', { name: 'Engine' })
    const items = within(engine).getAllByRole('listitem')
    const offl = items.find((li) => within(li).queryByText('OFFL'))
    expect(offl).toBeDefined()
    expect(within(offl!).getByText('disabled — not polled')).toHaveClass('text-muted')
    expect(offl).not.toHaveClass('border-warn')
    expect(within(engine).queryByText(/h behind/)).toBeNull()
    expect(items.at(-1)).toBe(offl) // after every polled symbol
  })

  it('reads a symbol row without `enabled` (an older publisher) as polled', async () => {
    const snap = snapshotWith()
    if (!('error' in snap.journal)) {
      snap.journal.engine = {
        ...snap.journal.engine,
        symbols: snap.journal.engine.symbols.map((row) => {
          const { enabled: _ignored, ...older } = row
          return older
        }),
      }
    }
    mockApi({ '/api/snapshot': { status: 200, body: snap } })
    renderApp('/')
    await screen.findByText('Engine live')
    const engine = screen.getByRole('region', { name: 'Engine' })
    expect(within(engine).getByText('2158 h behind')).toHaveClass('warn')
    expect(within(engine).queryByText('disabled — not polled')).toBeNull()
  })

  // D1: a publisher that cannot read sigengine/config.json sends
  // `enabled: null` (unknown). Unknown is not "disabled": the real lag shows.
  it('reads `enabled: null` (config unreadable at home) as polled, with its lag', async () => {
    const snap = snapshotWith()
    if (!('error' in snap.journal)) {
      snap.journal.engine = {
        ...snap.journal.engine,
        symbols: snap.journal.engine.symbols.map((row) => ({ ...row, enabled: null })),
      }
    }
    mockApi({ '/api/snapshot': { status: 200, body: snap } })
    renderApp('/')
    await screen.findByText('Engine live')
    const engine = screen.getByRole('region', { name: 'Engine' })
    expect(within(engine).queryByText('disabled — not polled')).toBeNull()
    expect(within(engine).getByText('2158 h behind')).toHaveClass('warn')
    expect(within(engine).getByText('60 min behind')).toBeInTheDocument()
    const order = within(engine)
      .getAllByRole('listitem')
      .map((li) => li.querySelector('.mono')?.textContent)
    expect(order).toEqual(['ACME', 'NOVA', 'OFFL', 'ORBT', 'QBIT/USD']) // as given: none sorted to the end
  })

  it('calls a running engine that stopped polling stalled, not live', async () => {
    const snap = snapshotWith()
    if (!('error' in snap.journal)) snap.journal.engine = { ...snap.journal.engine, polling: false }
    mockApi({ '/api/snapshot': { status: 200, body: snap } })
    renderApp('/')
    expect(await screen.findByText('Engine stalled')).toBeInTheDocument()
  })

  it('fills the executions blotter, including the option fill', async () => {
    mockApi()
    renderApp('/')
    const table = await screen.findByRole('table', { name: /Broker fills/ })
    expect(within(table).getAllByRole('row')).toHaveLength(9)
    const option = within(table).getByRole('rowheader', { name: /ORBT 20 Mar 26 40 put/ }).closest('tr')!
    expect(within(option).getByText('option')).toBeInTheDocument()
    expect(within(table).getByText('−$554.40')).toBeInTheDocument()
    expect(screen.getByText('+$331.02')).toBeInTheDocument()
  })

  it('fills the journal with every decision', async () => {
    mockApi()
    renderApp('/')
    const table = await screen.findByRole('table', { name: /Engine journal/ })
    expect(within(table).getAllByRole('row')).toHaveLength(9)
    expect(within(table).getByText('risk_block')).toBeInTheDocument()
    expect(within(table).getByText(/max_open_positions reached/)).toBeInTheDocument()
    // The legacy ISO `ts` row is shown through `_ts`, in ET by default.
    expect(within(table).getByText('2 Mar 09:00')).toBeInTheDocument()
  })

  it('shows a builder error in its own panel and keeps the rest', async () => {
    mockApi({ '/api/snapshot': { status: 200, body: snapshotWith({ executions: { error: 'APIError: broker timeout' } }) } })
    renderApp('/')
    expect(await screen.findByText('APIError: broker timeout')).toBeInTheDocument()
    expect(screen.getByText('$102,161.29')).toBeInTheDocument()
  })

  it('draws the first symbol, switches symbol, and reports an unpublished chart', async () => {
    mockApi()
    renderApp('/')
    await waitFor(() => expect(chartCalls).toContainEqual(['setData', 'ACME', 220]))
    const symbols = screen.getByRole('group', { name: 'Symbol' })
    expect(within(symbols).getByRole('button', { name: 'ACME' })).toHaveAttribute('aria-pressed', 'true')

    await userEvent.click(within(symbols).getByRole('button', { name: 'QBIT/USD' }))
    await waitFor(() => expect(chartCalls.some((c) => c[0] === 'setData' && c[1] === 'QBIT/USD')).toBe(true))

    await userEvent.click(within(symbols).getByRole('button', { name: 'ORBT' }))
    expect(await screen.findByText(/No chart has been published for/)).toBeInTheDocument()
  })

  it('switches every time to Melbourne and remembers the choice', async () => {
    mockApi()
    renderApp('/')
    const table = await screen.findByRole('table', { name: /Engine journal/ })
    expect(within(table).getByRole('columnheader', { name: 'Time (ET)' })).toBeInTheDocument()
    await waitFor(() => expect(chartCalls.length).toBeGreaterThan(0))
    await userEvent.click(within(screen.getByRole('group', { name: 'Show times in' })).getByRole('button', { name: 'Melbourne' }))
    expect(within(table).getByRole('columnheader', { name: 'Time (Melbourne)' })).toBeInTheDocument()
    expect(within(table).getByText('3 Mar 01:00')).toBeInTheDocument()
    expect(chartCalls).toContainEqual(['setZone', 'melbourne'])
    expect(window.localStorage.getItem('alpaca-dashboard:zone')).toBe('melbourne')
  })

  it('shows a journal builder error in both the engine and journal panels', async () => {
    mockApi({ '/api/snapshot': { status: 200, body: snapshotWith({ journal: { error: 'OSError: journal unreadable' } }) } })
    renderApp('/')
    expect(await screen.findByText(/could not build the engine status/)).toBeInTheDocument()
    expect(screen.getByText(/could not build the journal/)).toBeInTheDocument()
    expect(screen.getAllByText('OSError: journal unreadable')).toHaveLength(2)
    // The rest of the terminal still renders.
    expect(screen.getByText('$102,161.29')).toBeInTheDocument()
  })

  it('clamps a long research overview and renders its bold markers as bold, not as asterisks', async () => {
    const snap = snapshotWith()
    const long = `**DECISION:** stay with ACME. ${'Breadth stayed narrow into the close. '.repeat(20)}`
    if (!('error' in snap.board)) snap.board.notes = { ...snap.board.notes, _: long }
    mockApi({ '/api/snapshot': { status: 200, body: snap } })
    renderApp('/')
    const toggle = await screen.findByRole('button', { name: 'Show the full overview' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    const bold = screen.getByText('DECISION:')
    expect(bold.tagName).toBe('STRONG')
    expect(bold.closest('p')).not.toHaveTextContent('**')
    await userEvent.click(toggle)
    expect(screen.getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true')
  })
})
