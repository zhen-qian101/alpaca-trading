import type { ReactNode } from 'react'

import {
  isBuilderError,
  type OptionsSleeve,
  type Position,
  type ShortPut,
  type SnapshotResponse,
} from '../../contract.ts'
import { BiasTag, BuilderErrorNote, Panel, Skeleton, TableScroll } from '../../components/ui.tsx'
import {
  direction,
  displaySymbol,
  money,
  parseOcc,
  price,
  qty,
  signedMoney,
  signedPct,
} from '../../lib/format.ts'
import { formatIsoDate } from '../../lib/time.ts'

const COLS = 8

/** "Room" before the line: near (< 0.5%) and through (< 0) are called out in words. */
function Room({ value, through, near }: { value: number | null | undefined; through: string; near: string }) {
  if (value == null) return <span className="muted">—</span>
  if (value < 0) {
    return (
      <span className="down">
        {signedPct(value)} <span className="font-sans text-xs">{through}</span>
      </span>
    )
  }
  if (value < 0.005) {
    return (
      <span className="warn">
        {signedPct(value)} <span className="font-sans text-xs">{near}</span>
      </span>
    )
  }
  return <span>{signedPct(value)}</span>
}

function EquityRow({ p }: { p: Position }) {
  return (
    <tr>
      <th scope="row">
        <span className="sym">{p.symbol}</span>{' '}
        {p.side === 'short' ? <span className="tag tag--short">short</span> : null} <BiasTag bias={p.bias} />
      </th>
      <td className="num">{qty(p.qty)}</td>
      <td className="num">{price(p.avg_entry_price)}</td>
      <td className="num">{price(p.current_price)}</td>
      <td className="num">{money(p.market_value)}</td>
      <td className={`num ${direction(p.unrealized_pl)}`}>
        {signedMoney(p.unrealized_pl)}
        <br />
        <span className="text-xs">{signedPct(p.unrealized_plpc)}</span>
      </td>
      <td className="num">
        {p.stop_price != null ? (
          <>
            <span className="font-sans text-xs text-muted">stop </span>
            {price(p.stop_price)}
          </>
        ) : (
          <span className="font-sans text-xs text-muted">no stop</span>
        )}
      </td>
      <td className="num">
        <Room value={p.stop_distance_pct} through="through stop" near="near stop" />
      </td>
    </tr>
  )
}

function underlyingDistance(put: ShortPut): number | null {
  if (put.strike_distance_pct != null) return put.strike_distance_pct
  if (put.underlying_price != null && put.strike) return put.underlying_price / put.strike - 1
  return null
}

function ShortPutRow({ put, underlying }: { put: ShortPut; underlying: string }) {
  const occ = parseOcc(put.symbol)
  const strike = put.strike ?? occ?.strike ?? null
  const expiry = put.expiry ?? occ?.expiry ?? null
  const unrealized = put.unrealized
  return (
    <tr>
      <th scope="row">
        <span className="sym">{displaySymbol(put.symbol)}</span> <span className="tag tag--short">short put</span>
        <div className="font-sans text-xs font-normal text-muted">
          expires {expiry ? formatIsoDate(expiry) : 'unknown'}
        </div>
      </th>
      <td className="num">
        {qty(-Math.abs(put.contracts))}
        <span className="font-sans text-xs text-muted"> ct</span>
      </td>
      <td className="num">{price(put.avg_entry_price)}</td>
      <td className="num">{price(put.current_price)}</td>
      <td className="num">{money(put.market_value)}</td>
      <td className={`num ${direction(unrealized)}`}>{signedMoney(unrealized)}</td>
      <td className="num">
        <span className="font-sans text-xs text-muted">strike </span>
        {price(strike)}
      </td>
      <td className="num">
        <Room value={underlyingDistance(put)} through="in the money" near="near strike" />
        {put.underlying_price != null ? (
          <div className="font-sans text-xs text-muted">
            {underlying} at <span className="mono">{price(put.underlying_price)}</span>
          </div>
        ) : null}
      </td>
    </tr>
  )
}

function SleeveRows({ options }: { options: SnapshotResponse['options'] }) {
  const note = (key: string, text: ReactNode, tone: 'muted' | 'warn' | 'error' = 'muted') => (
    <tr key={key} className="detail-row">
      <td colSpan={COLS} className={tone === 'muted' ? 'muted' : ''}>
        {tone === 'muted' ? text : <div className={`note note--${tone === 'warn' ? 'warn' : 'error'} mt-1`}>{text}</div>}
      </td>
    </tr>
  )

  let heading = 'Options sleeve'
  let mode: ReactNode = null
  const rows: ReactNode[] = []

  if (options === undefined) {
    rows.push(note('absent', 'This report carries no options data: the home box did not send the sleeve.'))
  } else if (options === null) {
    rows.push(note('none', 'No options sleeve is configured.'))
  } else if (isBuilderError(options)) {
    rows.push(note('error', <>The home box could not read the options sleeve: <span className="mono">{options.error}</span></>, 'error'))
  } else {
    const o: OptionsSleeve = options
    heading = `Options sleeve: cash-secured puts on ${o.underlying}`
    mode = !o.enabled ? (
      <span className="tag tag--neutral">off</span>
    ) : o.shadow ? (
      <span className="tag tag--info">shadow: picks trades, sends none</span>
    ) : (
      <span className="tag tag--ok">live</span>
    )
    if (o.short_put) rows.push(<ShortPutRow key="put" put={o.short_put} underlying={o.underlying} />)
    else rows.push(note('no-put', o.enabled ? 'No put is open.' : 'The sleeve is switched off; no put is open.'))
    if (o.assigned) {
      const a = o.assigned
      rows.push(
        note(
          'assigned',
          <>
            <strong>Assigned:</strong> <span className="mono">{qty(a.qty)}</span> {a.symbol} at{' '}
            <span className="mono">{price(a.basis ?? a.avg_price)}</span>, held in the share table above. The sleeve
            sells them at its next weekly run{a.sell_by ? ` (${formatIsoDate(a.sell_by)})` : ''}; there is no stop on
            them by design.
            {a.managed_by_engine
              ? null
              : ' Nothing on the home box is managing these shares (no adoption record): they need a hand.'}
          </>,
          a.managed_by_engine ? 'muted' : 'warn',
        ),
      )
    }
  }

  return (
    <tbody>
      <tr className="group-row">
        <th scope="rowgroup" colSpan={COLS}>
          <span className="flex flex-wrap items-center gap-2">
            {heading} {mode}
          </span>
        </th>
      </tr>
      {rows}
    </tbody>
  )
}

/** Open positions: the share book, then the options sleeve, in one table. */
export function PositionsPanel({ snapshot, stale }: { snapshot: SnapshotResponse | undefined; stale: boolean }) {
  const portfolio = snapshot?.portfolio
  const positions = portfolio && !isBuilderError(portfolio) ? portfolio.positions : []
  const meta = portfolio && !isBuilderError(portfolio) ? `${positions.length} open in the share book` : null

  return (
    <Panel id="positions" title="Open positions" meta={meta} stale={stale}>
      {!snapshot ? (
        <div className="panel__body">
          <Skeleton className="h-24" />
        </div>
      ) : (
        <>
          {isBuilderError(portfolio) ? (
            <div className="panel__body">
              <BuilderErrorNote what="the positions" message={portfolio.error} />
            </div>
          ) : null}
          <TableScroll label="Open positions">
            <table className="tbl tbl--sticky-first">
              <caption className="sr-only">
                Open share positions and the options sleeve. Room is how far price can fall before the stop for shares,
                or before the put goes in the money for options.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Position</th>
                  <th scope="col" className="num">Qty</th>
                  <th scope="col" className="num">Entry</th>
                  <th scope="col" className="num">Last</th>
                  <th scope="col" className="num">Value</th>
                  <th scope="col" className="num">Unrealized</th>
                  <th scope="col" className="num">Stop / strike</th>
                  <th scope="col" className="num">Room</th>
                </tr>
              </thead>
              <tbody>
                <tr className="group-row">
                  <th scope="rowgroup" colSpan={COLS}>
                    Share book
                  </th>
                </tr>
                {positions.length === 0 ? (
                  <tr className="detail-row">
                    <td colSpan={COLS} className="muted">
                      {isBuilderError(portfolio) ? 'Unavailable.' : 'Flat: no open share positions.'}
                    </td>
                  </tr>
                ) : (
                  positions.map((p) => <EquityRow key={p.symbol} p={p} />)
                )}
              </tbody>
              <SleeveRows options={snapshot.options} />
            </table>
          </TableScroll>
          <p className="m-0 px-3.5 py-2 text-xs text-muted">
            Stops are enforced by the engine at bar close, not resting at the broker. Room is how far price can fall
            before the stop (shares) or before the put goes in the money (options).
          </p>
        </>
      )}
    </Panel>
  )
}
