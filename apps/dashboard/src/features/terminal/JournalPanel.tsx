import { isBuilderError, type JournalRow, type SnapshotResponse } from '../../contract.ts'
import { BuilderErrorNote, Panel, Skeleton, TableScroll } from '../../components/ui.tsx'
import { displaySymbol, qty } from '../../lib/format.ts'
import { usePrefs } from '../../lib/prefs.ts'
import { formatDateTime, ZONES } from '../../lib/time.ts'

function detail(r: JournalRow): string {
  const parts: string[] = []
  if (r.action) parts.push(r.action)
  if (r.strategy) parts.push(r.strategy)
  if (typeof r.new_pick === 'string') parts.push(`into ${r.new_pick}`)
  if (r.qty != null) parts.push(`qty ${qty(r.qty)}`)
  if (Array.isArray(r.voids_ts)) parts.push(`voids ${r.voids_ts.length} earlier ${r.voids_ts.length === 1 ? 'entry' : 'entries'}`)
  const head = parts.join(', ')
  if (r.reason) return head ? `${head}: ${r.reason}` : r.reason
  return head
}

function statusTone(status: string): string {
  if (/error|reject|fail/.test(status)) return 'tag--bad'
  if (/ok|closed|filled|submitted/.test(status)) return 'tag--ok'
  if (/block|skip/.test(status)) return 'tag--warn'
  return 'tag--neutral'
}

/** Every engine decision, including the ones that placed nothing. */
export function JournalPanel({ snapshot, stale }: { snapshot: SnapshotResponse | undefined; stale: boolean }) {
  const { zone } = usePrefs()
  const journal = snapshot?.journal
  const failed = isBuilderError(journal)
  const rows = journal && !failed ? journal.rows : undefined

  return (
    <Panel
      id="journal"
      title="Signal journal"
      meta={rows ? <span>{rows.length} most recent decisions</span> : null}
      stale={stale}
    >
      {failed ? (
        <div className="panel__body">
          <BuilderErrorNote what="the journal" message={journal.error} />
        </div>
      ) : !rows ? (
        <div className="panel__body">
          <Skeleton className="h-40" />
        </div>
      ) : rows.length === 0 ? (
        <p className="note">The journal is empty.</p>
      ) : (
        <TableScroll label="Signal journal" tall>
          <table className="tbl">
            <caption className="sr-only">Engine journal, newest first.</caption>
            <thead>
              <tr>
                <th scope="col">Time ({ZONES[zone].label})</th>
                <th scope="col">Event</th>
                <th scope="col">Symbol</th>
                <th scope="col">Detail</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => {
                const sym = r.symbol ?? r.ticker
                return (
                  <tr key={`${r._ts ?? r.ts}-${r.event}-${i}`}>
                    <td className="mono whitespace-nowrap">{formatDateTime(r._ts, zone)}</td>
                    <th scope="row" className="mono font-normal whitespace-nowrap">
                      {r.event}
                    </th>
                    <td className="sym">{sym ? displaySymbol(sym) : '—'}</td>
                    <td className="min-w-[16rem] max-w-[36rem]">{detail(r) || <span className="muted">—</span>}</td>
                    <td>
                      {r.status ? <span className={`tag ${statusTone(r.status)}`}>{r.status}</span> : <span className="muted">—</span>}
                    </td>
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
