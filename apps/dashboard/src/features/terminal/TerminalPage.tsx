import { isStaleStatus, useSnapshotStatus } from '../../lib/status.ts'
import { useSnapshot } from '../../lib/queries.ts'
import { useDocumentTitle } from '../../lib/useDocumentTitle.ts'
import { AccountBand } from './AccountBand.tsx'
import { BoardPanel } from './BoardPanel.tsx'
import { ChartPanel } from './ChartPanel.tsx'
import { EnginePanel } from './EnginePanel.tsx'
import { ExecutionsPanel } from './ExecutionsPanel.tsx'
import { JournalPanel } from './JournalPanel.tsx'
import { PositionsPanel } from './PositionsPanel.tsx'

/**
 * The terminal. On a phone it reads top to bottom in order of urgency:
 * account, positions, engine, board, chart, then the two logs. On a wide
 * screen positions run full width, the chart takes the left 7/12 and the
 * engine and board stack beside it.
 */
export function TerminalPage() {
  useDocumentTitle('Terminal')
  const { data: snapshot } = useSnapshot()
  const stale = isStaleStatus(useSnapshotStatus())

  return (
    <>
      <h1 className="sr-only">Terminal</h1>
      <AccountBand snapshot={snapshot} stale={stale} />
      <div className="mt-4">
        <PositionsPanel snapshot={snapshot} stale={stale} />
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-12">
        <div className="flex min-w-0 flex-col gap-4 lg:col-span-5 lg:col-start-8 lg:row-start-1">
          <EnginePanel snapshot={snapshot} stale={stale} />
          <BoardPanel snapshot={snapshot} stale={stale} />
        </div>
        <div className="flex min-w-0 flex-col gap-4 lg:col-span-7 lg:col-start-1 lg:row-start-1">
          {snapshot ? (
            <ChartPanel symbols={snapshot.symbols} stale={stale} />
          ) : (
            <section className="panel" aria-label="Signal chart">
              <div className="panel__body">
                <div className="skeleton h-[26rem]" aria-hidden="true" />
              </div>
            </section>
          )}
        </div>
      </div>
      <div className="mt-4 grid gap-4 2xl:grid-cols-2">
        <ExecutionsPanel snapshot={snapshot} stale={stale} />
        <JournalPanel snapshot={snapshot} stale={stale} />
      </div>
    </>
  )
}
