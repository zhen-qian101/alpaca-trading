import { lazy, Suspense } from 'react'
import { useSearchParams } from 'react-router'

import { isBuilderError, type ChartData, type SymbolChart } from '../../contract.ts'
import { Age, BuilderErrorNote, ErrorNote, Panel, Skeleton } from '../../components/ui.tsx'
import { describeError, isNotFound } from '../../lib/api.ts'
import { price } from '../../lib/format.ts'
import { usePrefs } from '../../lib/prefs.ts'
import { useChart } from '../../lib/queries.ts'
import { formatDateTime, isoToEpoch, ZONES, type ZoneKey } from '../../lib/time.ts'
import { MARKER_FAMILY_LABEL, markerFamily } from './chart/chartMath.ts'

// lightweight-charts is most of the bundle: load it with the first chart.
const ChartCanvas = lazy(() => import('./ChartCanvas.tsx'))

function ZoneToggle() {
  const { zone, setZone } = usePrefs()
  return (
    <div className="seg" role="group" aria-label="Show times in">
      {(Object.keys(ZONES) as ZoneKey[]).map((key) => (
        <button key={key} type="button" aria-pressed={zone === key} onClick={() => setZone(key)}>
          {ZONES[key].label}
        </button>
      ))}
    </div>
  )
}

function SignalsList({ data }: { data: SymbolChart }) {
  const { zone } = usePrefs()
  if (data.markers.length === 0) return <p className="m-0 px-3.5 py-2 text-xs text-muted">No signals or fills on this window.</p>
  const markers = [...data.markers].reverse()
  return (
    <details className="disclosure border-t border-rule">
      <summary>
        Markers on this chart <span className="mono text-xs font-normal text-muted">({data.markers.length})</span>
      </summary>
      <ul className="m-0 max-h-60 list-none overflow-y-auto px-3.5 pb-3 text-[0.8125rem]">
        {markers.map((m, i) => {
          const family = markerFamily(m)
          return (
            <li key={`${m.time}-${m.text}-${i}`} className="flex flex-wrap gap-x-3 border-t border-rule py-1 first:border-t-0">
              <span className="mono text-muted">
                {formatDateTime(m.time, zone)} {ZONES[zone].label}
              </span>
              <span className="mono font-semibold">{m.text || '—'}</span>
              <span className="text-muted">{MARKER_FAMILY_LABEL[family]}</span>
            </li>
          )
        })}
      </ul>
    </details>
  )
}

function Legend({ bands }: { bands: boolean }) {
  return (
    <ul className="m-0 flex list-none flex-wrap gap-x-4 gap-y-1 px-3.5 py-2 text-xs text-muted">
      <li>
        <span className="legend-mark up" aria-hidden="true">▲</span>
        <span className="legend-mark down" aria-hidden="true">▼</span> strategy entry and exit
      </li>
      <li>
        <span className="legend-mark warn" aria-hidden="true">▼</span> account exit (rotation, reconcile)
      </li>
      <li>
        <span className="legend-mark" style={{ color: 'var(--fill-buy)' }} aria-hidden="true">●</span>
        <span className="legend-mark" style={{ color: 'var(--fill-sell)' }} aria-hidden="true">●</span> broker fill, buy
        and sell
      </li>
      {bands ? (
        <li>
          <span className="legend-mark" style={{ color: 'var(--info)' }} aria-hidden="true">—</span> Bollinger bands
        </li>
      ) : null}
    </ul>
  )
}

/** Candles + the strategies' own indicators and markers, for one symbol at a time. */
export function ChartPanel({ symbols, stale }: { symbols: string[]; stale: boolean }) {
  const { zone } = usePrefs()
  const [params, setParams] = useSearchParams()
  const requested = params.get('symbol')
  const symbol = requested && symbols.includes(requested) ? requested : (symbols[0] ?? null)
  const query = useChart(symbol)
  const payload = query.data
  const chart = payload && !isBuilderError(payload) ? (payload as ChartData) : null
  const data = chart && symbol ? chart.symbols[symbol] : undefined
  const builtAt = payload ? isoToEpoch(payload.as_of) : null
  const failing = query.isError && payload !== undefined

  const select = (s: string) => {
    const next = new URLSearchParams(params)
    next.set('symbol', s)
    setParams(next, { replace: true })
  }

  return (
    <Panel
      id="chart"
      title={symbol ? `Signal chart: ${symbol}` : 'Signal chart'}
      stale={stale || failing}
      meta={
        <>
          {data ? <span className="mono">{data.interval}</span> : null}
          {builtAt !== null ? <Age epoch={builtAt} prefix="built " /> : null}
        </>
      }
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-rule">
        <div className="symbol-tabs min-w-0 flex-1 border-b-0" role="group" aria-label="Symbol">
          {symbols.map((s) => (
            <button key={s} type="button" aria-pressed={s === symbol} onClick={() => select(s)}>
              {s}
            </button>
          ))}
        </div>
        <div className="px-3.5 py-1.5">
          <ZoneToggle />
        </div>
      </div>

      {symbols.length === 0 ? (
        <p className="note">No symbols are enabled in the engine config.</p>
      ) : query.isPending ? (
        <div className="panel__body">
          <Skeleton className="h-[22rem]" />
        </div>
      ) : query.isError && !payload ? (
        isNotFound(query.error) ? (
          <p className="note">
            No chart has been published for <span className="mono">{symbol}</span> yet. The home box uploads charts at
            most every 5 minutes.
          </p>
        ) : (
          <div className="panel__body">
            <ErrorNote error={query.error} title={`The ${symbol} chart did not load.`} />
          </div>
        )
      ) : payload && isBuilderError(payload) ? (
        <div className="panel__body">
          <BuilderErrorNote what={`the ${symbol} chart`} message={payload.error} />
        </div>
      ) : !data || !symbol ? (
        <p className="note">
          The chart file for <span className="mono">{symbol}</span> does not contain that symbol.
        </p>
      ) : (
        <>
          {failing ? (
            <p className="note note--warn m-0" role="status">
              Chart refresh failed: {describeError(query.error)} Showing the chart built at{' '}
              {formatDateTime(builtAt, zone)} {ZONES[zone].label}.
            </p>
          ) : null}
          <Suspense
            fallback={
              <div className="panel__body">
                <Skeleton className="h-[22rem]" />
              </div>
            }
          >
            <ChartCanvas symbol={symbol} data={data} />
          </Suspense>
          <p className="sr-only">
            {data.candles.length} bars of {data.interval} candles; last close {price(data.candles.at(-1)?.close)}.{' '}
            {data.markers.length} markers, listed below.
          </p>
          <Legend bands={data.bbUpper.length > 0} />
          <SignalsList data={data} />
        </>
      )}
    </Panel>
  )
}
