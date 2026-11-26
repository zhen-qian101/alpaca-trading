import { isBuilderError, type SnapshotResponse } from '../../contract.ts'
import { Age, BuilderErrorNote, LiveStatus, Panel, Skeleton } from '../../components/ui.tsx'
import { usePrefs } from '../../lib/prefs.ts'
import { formatDateTime, ZONES } from '../../lib/time.ts'
import { barStatus, engineState, pollOrder } from './engine.ts'

export function EnginePanel({ snapshot, stale }: { snapshot: SnapshotResponse | undefined; stale: boolean }) {
  const { zone } = usePrefs()
  const journal = snapshot?.journal
  const failed = isBuilderError(journal)
  const engine = journal && !failed ? journal.engine : undefined
  const state = engine ? engineState(engine) : null

  return (
    <Panel
      id="engine"
      title="Engine"
      stale={stale}
      meta={state ? <span className={`badge badge--${state.tone}`}>{state.label}</span> : null}
    >
      <LiveStatus message={state ? `${state.label}. ${state.detail}` : null} />
      {failed ? (
        <div className="panel__body">
          <BuilderErrorNote what="the engine status" message={journal.error} />
        </div>
      ) : !engine || !state ? (
        <div className="panel__body">
          <Skeleton className="h-16" />
        </div>
      ) : (
        <>
          <div className="panel__body">
            <p className="m-0 text-[0.8125rem]">{state.detail}</p>
            <dl className="m-0 mt-2 grid grid-cols-1 gap-x-6 gap-y-1 text-[0.8125rem] sm:grid-cols-3">
              <div className="flex gap-2 sm:block">
                <dt className="text-xs text-muted">Last poll</dt>
                <dd className="m-0">
                  <Age epoch={engine.last_poll} />
                </dd>
              </div>
              <div className="flex gap-2 sm:block">
                <dt className="text-xs text-muted">Newest bar</dt>
                <dd className="m-0 mono">
                  {formatDateTime(engine.newest_bar, zone)} {engine.newest_bar != null ? ZONES[zone].label : ''}
                </dd>
              </div>
              <div className="flex gap-2 sm:block">
                <dt className="text-xs text-muted">Data fallback</dt>
                <dd className="m-0">
                  {engine.yfinance_fallback.length === 0 ? (
                    <span className="text-muted">none</span>
                  ) : (
                    <span className="warn">
                      yfinance for <span className="mono">{engine.yfinance_fallback.join(', ')}</span>
                    </span>
                  )}
                </dd>
              </div>
            </dl>
          </div>
          {engine.symbols.length > 0 ? (
            <div className="border-t border-rule px-3.5 py-2.5">
              <h3 className="m-0 text-xs font-semibold text-muted">Newest bar per symbol ({ZONES[zone].label})</h3>
              <ul className="m-0 mt-1.5 grid list-none grid-cols-[repeat(auto-fill,minmax(11.5rem,1fr))] gap-1.5 p-0">
                {pollOrder(engine.symbols).map((s) => {
                  const b = barStatus(s)
                  return (
                    <li
                      key={s.symbol}
                      data-polled={b.tone === 'off' ? 'false' : undefined}
                      className={`flex items-baseline justify-between gap-2 rounded-[4px] border px-2 py-1 text-[0.8125rem] ${b.tone === 'lagging' ? 'border-warn' : 'border-rule'} ${b.tone === 'off' ? 'text-muted' : ''}`}
                    >
                      <span className={`sym mono ${b.tone === 'off' ? '' : 'font-bold'}`}>{s.symbol}</span>
                      <span className="text-right text-xs">
                        <span className="mono">{formatDateTime(s.last_bar, zone)}</span>
                        <span className={`block ${b.tone === 'lagging' ? 'warn' : 'text-muted'}`}>{b.text}</span>
                      </span>
                    </li>
                  )
                })}
              </ul>
            </div>
          ) : null}
          <p className="m-0 px-3.5 py-2 text-xs text-muted">
            About one bar of lag on a single symbol is an expected transient; the engine re-pulls it at the end of the
            poll.
          </p>
          <details className="disclosure border-t border-rule">
            <summary>
              Engine log, last {engine.log_tail.length} {engine.log_tail.length === 1 ? 'line' : 'lines'}
              {engine.log_mtime != null ? (
                <span className="text-xs font-normal text-muted">
                  {' '}
                  (written <Age epoch={engine.log_mtime} />)
                </span>
              ) : null}
            </summary>
            {engine.log_tail.length > 0 ? (
              <pre className="logtail">{engine.log_tail.join('\n')}</pre>
            ) : (
              <p className="note">The log is empty.</p>
            )}
          </details>
        </>
      )}
    </Panel>
  )
}
