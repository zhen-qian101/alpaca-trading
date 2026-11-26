import { useId, useState } from 'react'

import { isBuilderError, type BoardRow, type Contingency, type SnapshotResponse } from '../../contract.ts'
import { Age, BiasTag, BuilderErrorNote, Panel, Skeleton, TableScroll } from '../../components/ui.tsx'
import { pct, price, qty } from '../../lib/format.ts'
import { usePrefs } from '../../lib/prefs.ts'
import { formatLongDateTime, formatIsoDate, isoToEpoch, ZONES } from '../../lib/time.ts'

const COLS = 5
/** Overviews longer than this start clamped to a few lines. */
const OVERVIEW_PREVIEW_CHARS = 360

/**
 * Research prose with its markdown **bold** shown as bold. Everything stays a
 * React text node: nothing from the notes is ever parsed as HTML.
 */
function Emphasised({ text }: { text: string }) {
  const parts = text.split(/\*\*(.+?)\*\*/g)
  return <>{parts.map((part, i) => (i % 2 === 1 ? <strong key={i}>{part}</strong> : part))}</>
}

/** The research overview (notes._): a few lines, expandable when it runs long. */
function Overview({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  const long = text.length > OVERVIEW_PREVIEW_CHARS
  return (
    <div className="px-3.5 pt-3 text-[0.8125rem]">
      <p id={id} className={`m-0 max-w-[72ch] whitespace-pre-line ${long && !open ? 'line-clamp-4' : ''}`}>
        <Emphasised text={text} />
      </p>
      {long ? (
        <button
          type="button"
          className="btn btn--quiet mt-1 min-h-0 px-0 py-1 text-xs"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen((v) => !v)}
        >
          {open ? 'Show less' : 'Show the full overview'}
        </button>
      ) : null}
    </div>
  )
}

function ContingencyLine({ c }: { c: Contingency }) {
  const { zone } = usePrefs()
  const until = isoToEpoch(c.valid_until)
  const side = c.condition === 'close_above' ? 'above' : 'below'
  return (
    <li className="py-1">
      <span className="text-ink">
        Go <strong>{c.action}</strong> if a bar closes {side} <span className="mono font-semibold">{price(c.level)}</span>
      </span>
      <span className="text-muted">
        , valid until{' '}
        <span className="mono">
          {formatLongDateTime(until, zone)} {ZONES[zone].label}
        </span>
      </span>
      {c.reason ? <p className="m-0 mt-0.5 text-muted">{c.reason}</p> : null}
    </li>
  )
}

function Row({ row, held }: { row: BoardRow; held: boolean }) {
  const live = row.priority > 0
  return (
    <>
      <tr>
        <td className={`num ${live ? 'font-bold' : 'muted'}`}>{row.priority}</td>
        <th scope="row">
          <span className="sym">{row.symbol}</span> {held ? <span className="tag tag--info">held</span> : null}
        </th>
        <td>
          <BiasTag bias={row.bias} />
        </td>
        <td className="num">{row.suggested_size != null ? pct(row.suggested_size, 0) : '—'}</td>
        <td className="num muted">{qty(row.max_position)}</td>
      </tr>
      {row.contingencies.length > 0 ? (
        // Hidden below 40rem, where the table scrolls sideways and would cut
        // every line of it off: ContingencyList shows the same text there.
        <tr className="detail-row max-sm:hidden">
          <td />
          <td colSpan={COLS - 1}>
            <span className="sr-only">Contingencies for {row.symbol}:</span>
            <ul className="m-0 list-none p-0 text-[0.8125rem]">
              {row.contingencies.map((c, i) => (
                <ContingencyLine key={`${c.condition}-${c.level}-${i}`} c={c} />
              ))}
            </ul>
          </td>
        </tr>
      ) : null}
    </>
  )
}

/**
 * The contingencies on a phone. Inside the table they share a cell with four
 * columns that already overflow a 360 px screen, so each line was cut at the
 * right edge and had to be panned sideways to read. Below 40rem they are
 * written out here, under the table, at the panel's full width, and the
 * table's detail rows are hidden (only one copy is ever displayed, so only
 * one reaches assistive tech).
 */
function ContingencyList({ rows }: { rows: BoardRow[] }) {
  const withAny = rows.filter((r) => r.contingencies.length > 0)
  if (withAny.length === 0) return null
  return (
    <div className="border-t border-rule px-3.5 py-2.5 sm:hidden" data-testid="contingencies-narrow">
      <h3 className="m-0 text-xs font-semibold text-muted">Contingencies</h3>
      <dl className="m-0">
        {withAny.map((row) => (
          <div key={row.symbol} className="pt-1.5">
            <dt className="sym mono text-[0.8125rem] font-bold">{row.symbol}</dt>
            <dd className="m-0">
              <ul className="m-0 list-none p-0 text-[0.8125rem] text-muted">
                {row.contingencies.map((c, i) => (
                  <ContingencyLine key={`${c.condition}-${c.level}-${i}`} c={c} />
                ))}
              </ul>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

/** decisions.json as the rotation board: candidates by priority, every contingency written out. */
export function BoardPanel({ snapshot, stale }: { snapshot: SnapshotResponse | undefined; stale: boolean }) {
  const board = snapshot?.board
  const portfolio = snapshot?.portfolio
  const held = new Set(portfolio && !isBuilderError(portfolio) ? portfolio.positions.map((p) => p.symbol) : [])

  const meta =
    board && !isBuilderError(board) ? (
      <>
        <span>research {formatIsoDate(board.date)}</span>
        {board.mtime != null ? <Age epoch={board.mtime} prefix="written " /> : null}
      </>
    ) : null

  return (
    <Panel id="board" title="Rotation board" meta={meta} stale={stale}>
      {!snapshot ? (
        <div className="panel__body">
          <Skeleton className="h-32" />
        </div>
      ) : isBuilderError(board) || !board ? (
        <div className="panel__body">
          <BuilderErrorNote what="the rotation board" message={board?.error ?? 'no board section'} />
        </div>
      ) : (
        <>
          {board.notes._ ? <Overview text={board.notes._} /> : null}
          <dl className="m-0 flex flex-wrap gap-x-6 gap-y-1 px-3.5 py-2.5 text-xs">
            <div className="flex gap-1.5">
              <dt className="text-muted">Max open positions</dt>
              <dd className="m-0 mono">{board.max_open_positions ?? '—'}</dd>
            </div>
            <div className="flex gap-1.5">
              <dt className="text-muted">Daily loss limit</dt>
              <dd className="m-0 mono">{board.daily_loss_limit != null ? pct(board.daily_loss_limit, 0) : '—'}</dd>
            </div>
            <div className="flex gap-1.5">
              <dt className="text-muted">Extended hours</dt>
              <dd className="m-0 mono">{board.allow_extended_hours == null ? '—' : board.allow_extended_hours ? 'on' : 'off'}</dd>
            </div>
          </dl>
          {board.rows.length === 0 ? (
            <p className="note">The watchlist is empty.</p>
          ) : (
            <TableScroll label="Rotation board">
              <table className="tbl">
                <caption className="sr-only">
                  Rotation candidates, highest priority first. Priority above zero is a live candidate. If taken is the
                  share of equity, after the put sleeve's reserve, that one position in that name would deploy.
                </caption>
                <thead>
                  <tr>
                    <th scope="col" className="num">Priority</th>
                    <th scope="col">Symbol</th>
                    <th scope="col">Bias</th>
                    <th scope="col" className="num">If taken</th>
                    <th scope="col" className="num">Max shares</th>
                  </tr>
                </thead>
                <tbody>
                  {board.rows.map((row) => (
                    <Row key={row.symbol} row={row} held={held.has(row.symbol)} />
                  ))}
                </tbody>
              </table>
            </TableScroll>
          )}
          <ContingencyList rows={board.rows} />
          <p className="m-0 px-3.5 py-2 text-xs text-muted">
            If taken: the share of equity, after the put sleeve's reserve, that one position in that name would
            deploy. The book holds one position at a time, so these do not add up.
          </p>
          <NotesList notes={board.notes} />
        </>
      )}
    </Panel>
  )
}

function NotesList({ notes }: { notes: Record<string, string> }) {
  const keys = Object.keys(notes).filter((k) => k !== '_')
  if (keys.length === 0) return null
  return (
    <details className="disclosure border-t border-rule">
      <summary>Research notes by symbol</summary>
      <dl className="m-0 px-3.5 pb-3">
        {keys.map((k) => (
          <div key={k} className="py-1.5">
            <dt className="sym mono font-bold">{k}</dt>
            <dd className="m-0 max-w-[72ch] whitespace-pre-line text-[0.8125rem]">
              <Emphasised text={notes[k]} />
            </dd>
          </div>
        ))}
      </dl>
    </details>
  )
}
