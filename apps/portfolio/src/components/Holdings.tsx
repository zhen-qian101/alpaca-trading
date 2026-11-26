import { useId } from 'react'

import type { Portfolio, Signals } from '../data/portfolio.ts'
import { longDate, money, pct, pctShort, price, quantity, etShortDateTime } from '../lib/format.ts'
import { liveContingencies, shareOfEquity, sortSignals, stopLevel } from '../lib/stats.ts'
import { Link } from '../lib/router.tsx'
import { LedgerTable } from './LedgerTable.tsx'

const POSITIONS_NOTE =
  'Entry is the account’s own average fill price. The stop is a check, not an order at the broker: once one of the symbol’s bars closes at or below the level, the engine sells at the next available price, so a gap or a fast bar can take the sale well below it. The level is approximate: the engine measures from the entry of its strategy’s simulated trade, the close of the bar the buy signal fired on, not from this fill. A dash means the site cannot vouch for a stop: shares the options sleeve was assigned (they have none, and the sleeve sells them at its next weekly run), a symbol the engine is not trading, or a position the engine did not open (a strategy that is long in its own simulation can still stop it out). A level shown is not a promise either: if a strategy’s simulation enters and stops out on the same bar, the engine handles the stop before the buy, and the position can be left with no stop. Unrealized profit and loss is not shown: it would give away the market price, which this site’s own figures leave out.'

export function Unavailable({ what }: { what: string }) {
  return <p className="muted">The {what} did not report in the latest export, so nothing is shown for it.</p>
}

/**
 * Open share/crypto positions and the short put sleeve: own fills and the
 * stop rule only. No unrealized P&L or distance to the stop: each is worked
 * out from the market price, and with the entry price it would give it away.
 * A stop is shown only where the site can vouch for one (the exporter's
 * armed_stop); a dash says it cannot, not that the engine holds none. The
 * stop follows a strategy's simulated trade, not the broker's position.
 */
export function Positions({ positions }: { positions: Portfolio['positions'] }) {
  const headingId = useId()
  const { equities, options } = positions
  const flat = equities !== null && equities.length === 0 && (options === null || options.length === 0)

  return (
    <section className="section" aria-labelledby={headingId}>
      <h2 id={headingId}>Open positions</h2>
      {equities === null ? (
        <Unavailable what="positions" />
      ) : flat ? (
        <p>
          None. The account is in cash, waiting for a buy signal on a symbol research allows.{' '}
          <Link to="/strategy">How entries work</Link>.
        </p>
      ) : equities.length ? (
        <LedgerTable
          label="Open positions"
          note={POSITIONS_NOTE}
        >
            <thead>
              <tr>
                <th scope="col">Symbol</th>
                <th scope="col" className="n">
                  Quantity
                </th>
                <th scope="col" className="n col">
                  Entry
                </th>
                <th scope="col" className="n col">
                  Stop
                </th>
              </tr>
            </thead>
            <tbody>
              {equities.map((p) => {
                const level = stopLevel(p)
                return (
                  <tr key={p.symbol}>
                    <th scope="row">
                      {p.symbol}
                      {p.side === 'short' ? <span className="loss"> (short)</span> : null}
                    </th>
                    <td className="n">{quantity(p.qty)}</td>
                    <td className="n col">{price(p.avg_entry_price)}</td>
                    <td className="n col">
                      {/* The book sets no stop on a short (it holds none by design). */}
                      {level !== null ? (
                        <>
                          {price(level)}
                          <span className="block small muted">{pctShort(p.stop_pct)} under</span>
                        </>
                      ) : (
                        '–'
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
        </LedgerTable>
      ) : (
        <p>No share or crypto positions are open.</p>
      )}

      {options === null && equities !== null ? <Unavailable what="options sleeve" /> : null}
      {options && options.length ? (
        <div className="section">
          <h3>Options sleeve</h3>
          <ul className="prose m-0 list-disc pl-5">
            {options.map((o, i) => (
              <li key={`${o.underlying}-${o.expiry ?? ''}-${o.strike ?? ''}-${i}`}>
                Short {quantity(o.qty)} {o.underlying} {o.type}
                {o.qty === 1 ? '' : 's'}
                {o.strike !== null ? <>, strike {price(o.strike)}</> : null}
                {o.expiry ? <>, expires {longDate(o.expiry)}</> : null}
                {o.premium_received !== null ? (
                  <>
                    . Premium received <span className="num">{money(o.premium_received)}</span>.
                  </>
                ) : (
                  '.'
                )}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  )
}

function biasLabel(bias: 'long' | 'flat' | 'short' | null): string {
  if (bias === 'long') return 'Long'
  if (bias === 'short') return 'Short'
  if (bias === 'flat') return 'Flat'
  return 'Not set'
}

/** How the board's size column relates to the whole account, in one sentence. */
function sizeNote(rows: readonly Signals['rows'][number][], sleeveReserve: number | null): string {
  if (sleeveReserve === 0) return 'Size is the share of equity a buy would use.'
  if (sleeveReserve === null)
    return 'Size is the share of the share book a buy would use: equity less any reserve the options sleeve keeps back.'
  const size = Math.max(0, ...rows.map((r) => r.suggested_size ?? 0))
  const whole = shareOfEquity(size, sleeveReserve)
  const example = size > 0 && whole !== null ? ` A size of ${pctShort(size)} is about ${pct(whole, 0)} of equity.` : ''
  return (
    `Size is the share of the share book a buy would use: equity less the ${pctShort(sleeveReserve)} the options ` +
    `sleeve keeps back.${example}`
  )
}

/** The research board: what research allows, not what the strategies have done. */
export function SignalBoard({
  signals,
  nowMs,
  sleeveReserve = null,
}: {
  signals: Signals
  nowMs: number
  /** strategy.risk.sleeve_reserve_pct, when the strategy section reported. */
  sleeveReserve?: number | null
}) {
  const headingId = useId()
  const rows = sortSignals(signals.rows)
  return (
    <section className="section" aria-labelledby={headingId}>
      <h2 id={headingId}>Research board</h2>
      <p className="small muted prose">
        Research marks each watched symbol long (buys allowed) or flat (no new buys). Among longs, the higher
        priority wins the single position slot. {sizeNote(rows, sleeveReserve)} The strategies still decide when to
        trade.
      </p>
      {rows.length === 0 ? (
        <p>No symbols are on the board.</p>
      ) : (
        <LedgerTable
          label="Research board"
          note={signals.date ? `Board for the session of ${longDate(signals.date)}.` : 'Latest board.'}
        >
            <thead>
              <tr>
                <th scope="col">Symbol</th>
                <th scope="col">Bias</th>
                <th scope="col" className="n col">
                  Priority
                </th>
                <th scope="col" className="n">
                  Size
                </th>
                <th scope="col" className="col">
                  Standing order
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const live = liveContingencies(row, nowMs)
                const isLong = row.bias === 'long'
                return (
                  <tr key={row.symbol}>
                    <th scope="row">{row.symbol}</th>
                    <td>
                      <span className={`bias bias--${row.bias ?? 'flat'}`}>{biasLabel(row.bias)}</span>
                    </td>
                    <td className={`n col ${isLong ? '' : 'muted'}`}>{row.priority ?? '–'}</td>
                    <td className={`n ${isLong ? '' : 'muted'}`}>
                      {row.suggested_size ? pct(row.suggested_size, 0) : '–'}
                    </td>
                    <td className="col wrap-cell">
                      {live.length ? (
                        live.map((c, i) => (
                          <span key={i} className="block">
                            Buy allowed on a close {c.condition === 'close_above' ? 'above' : 'below'}{' '}
                            <span className="num">{price(c.level)}</span>, until {etShortDateTime(c.valid_until)}
                          </span>
                        ))
                      ) : (
                        <span className="muted">None</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
        </LedgerTable>
      )}
    </section>
  )
}
