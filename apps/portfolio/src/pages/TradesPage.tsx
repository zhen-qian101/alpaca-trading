import { useEffect, useId, useMemo, useRef, useState } from 'react'

import { Unavailable } from '../components/Holdings.tsx'
import { Page } from '../components/Layout.tsx'
import { LedgerTable } from '../components/LedgerTable.tsx'
import { LOADING } from '../lib/copy.ts'
import { usePortfolio } from '../data/portfolioContext.ts'
import type { RoundTrip, Trades } from '../data/portfolio.ts'
import {
  decimal,
  etShortDateTime,
  holding,
  pct,
  price,
  quantity,
  signedMoney,
  signedPct,
  signedPctShort,
  tone,
} from '../lib/format.ts'
import { returnHistogram, sortTrades, type SortDir, type TradeSortKey } from '../lib/stats.ts'

const STRATEGY_NAMES: Record<NonNullable<RoundTrip['strategy']>, string> = {
  ema: 'EMA cross',
  bollinger_rsi: 'Bollinger + RSI',
  macd_sma: 'MACD + SMA',
}

function Summary({ trades }: { trades: Trades }) {
  const s = trades.stats
  return (
    <section className="section" aria-label="Summary">
      <dl className="leaders">
        <div>
          <dt>Closed round trips</dt>
          <dd>{s.round_trips}</dd>
        </div>
        <div>
          <dt>Wins and losses</dt>
          <dd>
            {s.wins} won, {s.losses} lost
          </dd>
        </div>
        <div>
          <dt>Win rate</dt>
          <dd>{pct(s.win_rate, 0)}</dd>
        </div>
        <div>
          <dt>Average win</dt>
          <dd className={tone(s.avg_win)}>
            {signedMoney(s.avg_win)} <span className="muted">({signedPct(s.avg_win_pct)})</span>
          </dd>
        </div>
        <div>
          <dt>Average loss</dt>
          <dd className={tone(s.avg_loss)}>
            {signedMoney(s.avg_loss)} <span className="muted">({signedPct(s.avg_loss_pct)})</span>
          </dd>
        </div>
        <div>
          <dt>Profit factor</dt>
          <dd>{s.profit_factor === null ? (s.wins > 0 ? 'No losses yet' : '–') : decimal(s.profit_factor)}</dd>
        </div>
        <div className="total">
          <dt>Realized profit and loss</dt>
          <dd className={tone(s.realized_pnl)}>{signedMoney(s.realized_pnl)}</dd>
        </div>
      </dl>
      <p className="small muted prose">
        Profit factor is gross profit divided by gross loss: above 1 means the wins have outweighed the losses. Figures
        cover share and crypto trades; the options sleeve is not included. A crypto trip’s result counts the coins the
        broker keeps as its fee on the buy; a fee charged in dollars on the sale is not deducted.
      </p>
    </section>
  )
}

/** A bin edge: two places at most, so 0.25%-wide bins read −0.75%, −0.5%, −0.25%, 0%, +0.25%. */
const pctLabel = (fraction: number) => signedPctShort(fraction)

/**
 * The width of an element in CSS pixels, so an SVG can be drawn at 1:1 and
 * its 12px labels stay 12px on a phone. Falls back to `fallback` where
 * ResizeObserver is missing.
 */
function useWidth(fallback: number) {
  const ref = useRef<HTMLElement>(null)
  const [width, setWidth] = useState(fallback)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(([entry]) => {
      const w = Math.round(entry?.contentRect.width ?? 0)
      if (w > 0) setWidth(Math.min(Math.max(w, 280), 704))
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return [ref, width] as const
}

function Distribution({ trips }: { trips: readonly RoundTrip[] }) {
  const headingId = useId()
  const [figureRef, W] = useWidth(640)
  const { bins } = useMemo(() => returnHistogram(trips.map((t) => t.pnl_pct)), [trips])
  if (!bins.length) return null

  const maxCount = Math.max(...bins.map((b) => b.count), 1)
  const H = W < 480 ? 200 : 240
  const pad = { top: 22, right: 8, bottom: 40, left: 8 }
  const plotW = W - pad.left - pad.right
  const plotH = H - pad.top - pad.bottom
  const step = plotW / bins.length
  const barW = Math.max(step - 4, 2)
  const zeroIndex = bins.findIndex((b) => b.from >= 0)
  const zeroX = pad.left + (zeroIndex < 0 ? bins.length : zeroIndex) * step
  // Label every bin edge when there is room, otherwise every other one.
  const labelEvery = step < 44 ? Math.ceil(44 / step) : 1

  const wins = trips.filter((t) => t.pnl_pct > 0).length
  const losses = trips.filter((t) => t.pnl_pct < 0).length
  const tallest = bins.reduce((a, b) => (b.count > a.count ? b : a))
  const description =
    `Histogram of ${trips.length} round-trip returns: ${wins} above zero, ${losses} below. ` +
    `The most common range is ${pctLabel(tallest.from)} to ${pctLabel(tallest.to)} with ${tallest.count} trade${tallest.count === 1 ? '' : 's'}.`

  return (
    <section className="section" aria-labelledby={headingId}>
      <h2 id={headingId}>How the returns are spread</h2>
      <p className="small muted prose">Each bar counts the round trips whose return fell in that range. Red bars are losses.</p>
      <figure className="m-0" ref={figureRef}>
        <svg className="histogram" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={description}>
          <line className="axis" x1={pad.left} x2={W - pad.right} y1={pad.top + plotH} y2={pad.top + plotH} />
          {bins.map((b, i) => {
            const h = (b.count / maxCount) * plotH
            const x = pad.left + i * step + (step - barW) / 2
            const y = pad.top + plotH - h
            const loss = b.to <= 0
            return (
              <g key={i}>
                {b.count > 0 ? (
                  <>
                    <rect className={loss ? 'bar-loss' : 'bar-gain'} x={x} y={y} width={barW} height={h} rx={1.5} />
                    <text x={x + barW / 2} y={y - 6} textAnchor="middle" fontSize="12">
                      {b.count}
                    </text>
                  </>
                ) : null}
              </g>
            )
          })}
          <line className="zero" x1={zeroX} x2={zeroX} y1={pad.top - 6} y2={pad.top + plotH + 6} strokeWidth={1.5} />
          {bins.map((b, i) =>
            i % labelEvery === 0 || b.from === 0 ? (
              <text key={`l${i}`} x={pad.left + i * step} y={H - pad.bottom + 20} textAnchor="middle" fontSize="12">
                {pctLabel(b.from)}
              </text>
            ) : null,
          )}
          {bins.length % labelEvery === 0 ? (
            <text x={pad.left + bins.length * step} y={H - pad.bottom + 20} textAnchor="middle" fontSize="12">
              {pctLabel(bins.at(-1)!.to)}
            </text>
          ) : null}
          <text x={W - pad.right} y={H - 4} textAnchor="end" fontSize="12">
            Return per round trip
          </text>
        </svg>
        <figcaption className="visually-hidden">{description}</figcaption>
      </figure>
      <details className="data-table">
        <summary>The same counts, as a table</summary>
        <LedgerTable label="Return ranges" note="Round trips by return range." className="max-w-96">
          <thead>
            <tr>
              <th scope="col">Return from</th>
              <th scope="col">up to</th>
              <th scope="col" className="n col">
                Trips
              </th>
            </tr>
          </thead>
          <tbody>
            {bins.map((b, i) => (
              <tr key={i}>
                <td>{pctLabel(b.from)}</td>
                <td>{pctLabel(b.to)}</td>
                <td className="n col">{b.count}</td>
              </tr>
            ))}
          </tbody>
        </LedgerTable>
      </details>
    </section>
  )
}

interface Column {
  key: TradeSortKey
  label: string
  numeric: boolean
  /** First click sorts this way. */
  firstDir: SortDir
}

const SORTABLE: Record<TradeSortKey, Column> = {
  symbol: { key: 'symbol', label: 'Symbol', numeric: false, firstDir: 'asc' },
  entry_at: { key: 'entry_at', label: 'Opened', numeric: false, firstDir: 'desc' },
  exit_at: { key: 'exit_at', label: 'Closed', numeric: false, firstDir: 'desc' },
  held: { key: 'held', label: 'Held', numeric: true, firstDir: 'desc' },
  pnl_pct: { key: 'pnl_pct', label: 'Return', numeric: true, firstDir: 'desc' },
  pnl: { key: 'pnl', label: 'P&L', numeric: true, firstDir: 'desc' },
}

function SortHeader({
  column,
  sort,
  onSort,
  className = '',
}: {
  column: Column
  sort: { key: TradeSortKey; dir: SortDir }
  onSort: (key: TradeSortKey) => void
  className?: string
}) {
  const active = sort.key === column.key
  return (
    <th
      scope="col"
      className={`${column.numeric ? 'n' : ''} ${className}`}
      aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : undefined}
    >
      <button type="button" className="sort-button" onClick={() => onSort(column.key)}>
        {column.label}
        <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true">
          <path d="M5 1 9 5H1Z" fill="currentColor" opacity={active && sort.dir === 'asc' ? 1 : 0.3} />
          <path d="M5 11 1 7h8Z" fill="currentColor" opacity={active && sort.dir === 'desc' ? 1 : 0.3} />
        </svg>
      </button>
    </th>
  )
}

function TradeTable({ trades }: { trades: Trades }) {
  const headingId = useId()
  const [sort, setSort] = useState<{ key: TradeSortKey; dir: SortDir }>({ key: 'exit_at', dir: 'desc' })
  const rows = useMemo(() => sortTrades(trades.round_trips, sort.key, sort.dir), [trades, sort])
  const showStrategy = trades.round_trips.some((t) => t.strategy !== null)
  const total = trades.round_trips.reduce((sum, t) => sum + t.pnl, 0)
  const onSort = (key: TradeSortKey) =>
    setSort((prev) =>
      prev.key === key ? { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: SORTABLE[key].firstDir },
    )
  const sortedBy = `${SORTABLE[sort.key].label.toLowerCase()}, ${sort.dir === 'asc' ? 'ascending' : 'descending'}`

  return (
    <section className="section" aria-labelledby={headingId}>
      <h2 id={headingId}>Every round trip</h2>
      {rows.length === 0 ? (
        <p>No round trip has closed yet.</p>
      ) : (
        <LedgerTable
          label="Round trips"
          note={`${rows.length} closed round trips at the account’s own fill prices, sorted by ${sortedBy}. Times are New York time. Choose a column heading to sort.`}
        >
            {/* Return and P&L come straight after the symbol: on a phone only
                the first few columns fit, and those are the figures the table
                is for. The rest scroll in from the right. */}
            <thead>
              <tr>
                <SortHeader column={SORTABLE.symbol} sort={sort} onSort={onSort} />
                <SortHeader column={SORTABLE.pnl_pct} sort={sort} onSort={onSort} className="col" />
                <SortHeader column={SORTABLE.pnl} sort={sort} onSort={onSort} />
                <SortHeader column={SORTABLE.entry_at} sort={sort} onSort={onSort} className="col" />
                <SortHeader column={SORTABLE.exit_at} sort={sort} onSort={onSort} />
                <SortHeader column={SORTABLE.held} sort={sort} onSort={onSort} />
                <th scope="col" className="n col">
                  Quantity
                </th>
                <th scope="col" className="n">
                  Entry
                </th>
                <th scope="col" className="n">
                  Exit
                </th>
                {showStrategy ? <th scope="col">Strategy</th> : null}
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <tr key={`${t.symbol}-${t.entry_at}-${t.exit_at}`}>
                  <th scope="row">{t.symbol}</th>
                  <td className={`n col ${tone(t.pnl_pct)}`}>{signedPct(t.pnl_pct)}</td>
                  <td className={`n ${tone(t.pnl)}`}>{signedMoney(t.pnl)}</td>
                  <td className="col">{etShortDateTime(t.entry_at)}</td>
                  <td>{etShortDateTime(t.exit_at)}</td>
                  <td className="n">{holding(t.entry_at, t.exit_at)}</td>
                  <td className="n col">{quantity(t.qty)}</td>
                  <td className="n">{price(t.entry_price)}</td>
                  <td className="n">{price(t.exit_price)}</td>
                  {showStrategy ? <td>{t.strategy ? STRATEGY_NAMES[t.strategy] : '–'}</td> : null}
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <th scope="row" colSpan={2} className="wrap">
                  Total of the trips listed
                </th>
                <td className={`n ${tone(total)}`}>{signedMoney(total)}</td>
                <td colSpan={showStrategy ? 7 : 6}>&nbsp;</td>
              </tr>
            </tfoot>
        </LedgerTable>
      )}
    </section>
  )
}

export function TradesPage() {
  const state = usePortfolio()
  const trades = state.status === 'ready' ? state.data.trades : null
  return (
    <Page
      title="Trades"
      heading="Trades"
      intro={
        <p className="lede">
          Every closed round trip, from the buy to the sell that took the position back to flat, at the account’s own
          fill prices. The book is long only, and its strategies hold one position at a time, entered and exited in
          one piece.
        </p>
      }
    >
      {state.status === 'loading' ? (
        <p className="muted">{LOADING}</p>
      ) : state.status === 'ready' && !trades ? (
        <Unavailable what="trade history" />
      ) : trades ? (
        <>
          <Summary trades={trades} />
          <Distribution trips={trades.round_trips} />
          <TradeTable trades={trades} />
        </>
      ) : (
        <p className="muted">Trades appear here once figures are published.</p>
      )}
    </Page>
  )
}
