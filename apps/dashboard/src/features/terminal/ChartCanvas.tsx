import { useEffect, useRef, useState } from 'react'

import type { SymbolChart } from '../../contract.ts'
import { direction, price } from '../../lib/format.ts'
import { usePrefs } from '../../lib/prefs.ts'
import { formatLongDateTime, ZONES, type ZoneKey } from '../../lib/time.ts'
import { readPalette, SignalChart, type CrosshairValues, type PaneLabel } from './chart/signalChart.ts'

function Readout({ values, zone }: { values: CrosshairValues | null; zone: ZoneKey }) {
  if (!values?.candle) return <p className="m-0 min-h-5 text-xs text-muted">Hover the chart for bar values.</p>
  const c = values.candle
  const dir = direction(c.close - c.open)
  return (
    <p className="m-0 flex min-h-5 flex-wrap gap-x-3 gap-y-0.5 text-xs">
      <span className="mono text-muted">
        {formatLongDateTime(values.time, zone)} {ZONES[zone].label}
      </span>
      <span>
        <span className="text-muted">O </span>
        <span className="mono">{price(c.open)}</span>
      </span>
      <span>
        <span className="text-muted">H </span>
        <span className="mono">{price(c.high)}</span>
      </span>
      <span>
        <span className="text-muted">L </span>
        <span className="mono">{price(c.low)}</span>
      </span>
      <span>
        <span className="text-muted">C </span>
        <span className={`mono ${dir}`}>{price(c.close)}</span>
      </span>
      {values.rsi != null ? (
        <span>
          <span className="text-muted">RSI </span>
          <span className="mono">{values.rsi.toFixed(1)}</span>
        </span>
      ) : null}
      {values.macd != null ? (
        <span>
          <span className="text-muted">MACD </span>
          <span className={`mono ${direction(values.macd)}`}>{price(values.macd, 3)}</span>
        </span>
      ) : null}
    </p>
  )
}

/**
 * Owns one SignalChart for as long as the panel is mounted. Data refreshes
 * reuse it (so zoom and pan survive); a different symbol redraws it.
 */
export default function ChartCanvas({ symbol, data }: { symbol: string; data: SymbolChart }) {
  const { zone, theme } = usePrefs()
  const hostRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<SignalChart | null>(null)
  const [hover, setHover] = useState<CrosshairValues | null>(null)
  const [panes, setPanes] = useState<PaneLabel[]>([])
  // The zone at mount; later changes go through setZone below.
  const zoneRef = useRef(zone)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const chart = new SignalChart(host, {
      zone: zoneRef.current,
      palette: readPalette(document.documentElement),
      onCrosshair: setHover,
      onPanes: setPanes,
    })
    chartRef.current = chart
    return () => {
      chart.destroy()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    chartRef.current?.setData(symbol, data)
  }, [symbol, data])

  useEffect(() => {
    chartRef.current?.setZone(zone)
  }, [zone])

  useEffect(() => {
    // The theme attribute is set by a parent effect, which runs after this
    // one: read the tokens on the next frame, once they have switched.
    const id = requestAnimationFrame(() => chartRef.current?.setPalette(readPalette(document.documentElement)))
    return () => cancelAnimationFrame(id)
  }, [theme])

  const last = data.candles.at(-1)
  const shown: CrosshairValues | null =
    hover ?? (last ? { time: last.time, candle: last, rsi: data.rsi.at(-1)?.value, macd: data.macdHist.at(-1)?.value } : null)

  return (
    <>
      <div className="px-3.5 pt-2">
        <Readout values={shown} zone={zone} />
      </div>
      <div className="chart-host mt-1" ref={hostRef} role="img" aria-label={`${symbol} ${data.interval} candlestick chart`}>
        {panes.map((p, i) => (
          <span key={i} className="chart-pane-label" style={{ top: p.top }}>
            {p.text}
          </span>
        ))}
      </div>
    </>
  )
}
