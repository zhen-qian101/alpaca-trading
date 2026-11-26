import { isBuilderError, type SnapshotResponse } from '../../contract.ts'
import { BuilderErrorNote, Panel, Skeleton, TableScroll } from '../../components/ui.tsx'
import { direction, displaySymbol, parseOcc, price, qty, signedMoney, signedPct } from '../../lib/format.ts'
import { usePrefs } from '../../lib/prefs.ts'
import { formatDateTime, ZONES } from '../../lib/time.ts'

/** Broker fills, newest first, with entries paired to exits (the blotter). */
export function ExecutionsPanel({ snapshot, stale }: { snapshot: SnapshotResponse | undefined; stale: boolean }) {
  const { zone } = usePrefs()
  const ex = snapshot?.executions

  const meta =
    ex && !isBuilderError(ex) ? (
      <span>
        realized <span className={`mono ${direction(ex.realized_pnl)}`}>{signedMoney(ex.realized_pnl)}</span> over{' '}
        <span className="mono">{ex.round_trips}</span> round {ex.round_trips === 1 ? 'trip' : 'trips'}
        {ex.win_rate != null ? (
          <>
            , <span className="mono">{Math.round(ex.win_rate * 100)}%</span> won
          </>
        ) : null}
      </span>
    ) : null

  return (
    <Panel id="executions" title="Executions" meta={meta} stale={stale}>
      {!snapshot ? (
        <div className="panel__body">
          <Skeleton className="h-40" />
        </div>
      ) : isBuilderError(ex) || !ex ? (
        <div className="panel__body">
          <BuilderErrorNote what="the executions" message={ex?.error ?? 'no executions section'} />
        </div>
      ) : ex.rows.length === 0 ? (
        <p className="note">No executed orders yet.</p>
      ) : (
        <TableScroll label="Executions" tall>
          <table className="tbl">
            <caption className="sr-only">
              Broker fills, newest first. A buy realizes nothing; the profit or loss lands on the sell that closes it.
            </caption>
            <thead>
              <tr>
                <th scope="col">Filled ({ZONES[zone].label})</th>
                <th scope="col">Symbol</th>
                <th scope="col">Side</th>
                <th scope="col" className="num">Qty</th>
                <th scope="col" className="num">Entry</th>
                <th scope="col" className="num">Exit</th>
                <th scope="col" className="num">Realized</th>
                <th scope="col">Type</th>
              </tr>
            </thead>
            <tbody>
              {ex.rows.map((r, i) => {
                const option = parseOcc(r.symbol) !== null
                return (
                  <tr key={`${r.ts ?? 'x'}-${r.symbol}-${i}`}>
                    <td className="mono whitespace-nowrap">{formatDateTime(r.ts, zone)}</td>
                    <th scope="row">
                      <span className="sym">{displaySymbol(r.symbol)}</span>{' '}
                      {option ? <span className="tag tag--info">option</span> : null}
                    </th>
                    <td>
                      <span className={`tag ${r.side === 'buy' ? 'tag--long' : 'tag--short'}`}>{r.side}</span>
                    </td>
                    <td className="num">{qty(r.qty)}</td>
                    <td className="num">{price(r.entry_price)}</td>
                    <td className="num">{price(r.exit_price)}</td>
                    <td className={`num ${direction(r.pnl)}`}>
                      {r.pnl == null ? (
                        <span className="muted">—</span>
                      ) : (
                        <>
                          {signedMoney(r.pnl)}
                          {r.pnl_pct != null ? <span className="text-xs"> ({signedPct(r.pnl_pct)})</span> : null}
                        </>
                      )}
                    </td>
                    <td className="muted">{r.order_type}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </TableScroll>
      )}
    </Panel>
  )
}
