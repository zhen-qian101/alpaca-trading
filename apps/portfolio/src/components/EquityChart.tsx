/**
 * The account's own equity, drawn with TradingView Lightweight Charts v5 as a
 * baseline area: above the starting equity in ink, below it in red ink. Only
 * the account's own values are plotted; there is no market data here.
 *
 * One point per session: the file carries no intraday series (owner
 * decision), because with a position open, equity sampled through the day
 * divided by its quantity would trace that symbol's price.
 *
 * The library is loaded on demand so the page's text renders first. Its
 * attribution logo stays on (the library's licence asks for attribution).
 * A text summary and a table of every point are the chart's text alternative.
 */
import { useEffect, useId, useMemo, useRef, useState } from 'react'

import type { Account } from '../data/portfolio.ts'
import { longDate, money, signedMoney, signedPct, tone } from '../lib/format.ts'
import { summarizeCurve } from '../lib/stats.ts'
import { LedgerTable } from './LedgerTable.tsx'
import { useTheme } from '../lib/themeContext.ts'

interface Point {
  /** The session date, "YYYY-MM-DD". */
  time: string
  value: number
  label: string
}

interface Series {
  points: Point[]
  base: number
  baseLabel: string
  summary: string
}

function dailySeries(account: Account): Series | null {
  const curve = summarizeCurve(account.daily)
  if (!curve) return null
  const base = account.start_equity ?? curve.first.equity
  const points = account.daily.map((p) => ({ time: p.date, value: p.equity, label: longDate(p.date) }))
  const summary =
    `Daily closing equity from ${longDate(curve.first.date)} (${money(curve.first.equity)}) ` +
    `to ${longDate(curve.last.date)} (${money(curve.last.equity)}). ` +
    `Highest close ${money(curve.high.equity)} on ${longDate(curve.high.date)}; ` +
    `lowest ${money(curve.low.equity)} on ${longDate(curve.low.date)}.`
  return { points, base, baseLabel: 'Start', summary }
}

/** The key a chart time is filed under: lightweight-charts may hand back a string, a number or a business-day object. */
function timeKey(time: unknown): string {
  if (typeof time === 'object' && time !== null && 'year' in time && 'month' in time && 'day' in time) {
    const t = time as { year: number; month: number; day: number }
    return `${t.year}-${String(t.month).padStart(2, '0')}-${String(t.day).padStart(2, '0')}`
  }
  return String(time)
}

function cssVar(name: string, fallback: string): string {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
  } catch {
    return fallback
  }
}

function withAlpha(hex: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex)
  if (!m) return hex
  const n = Number.parseInt(m[1]!, 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}

function DataTable({ series }: { series: Series }) {
  const rows = [...series.points].reverse()
  return (
    <details className="data-table">
      <summary>Equity by day, as a table</summary>
      <LedgerTable label="Equity values" note="Closing equity for each session, newest first.">
          <thead>
            <tr>
              <th scope="col">Session</th>
              <th scope="col" className="n col">
                Equity
              </th>
              <th scope="col" className="n col">
                Against {series.baseLabel.toLowerCase()}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const diff = p.value - series.base
              return (
                <tr key={String(p.time)}>
                  <th scope="row" className="font-normal">
                    {p.label}
                  </th>
                  <td className="n col">{money(p.value)}</td>
                  <td className={`n col ${tone(diff)}`}>{signedMoney(diff)}</td>
                </tr>
              )
            })}
          </tbody>
      </LedgerTable>
    </details>
  )
}

export function EquityChart({ account }: { account: Account }) {
  const series = useMemo(() => dailySeries(account), [account])
  const { theme } = useTheme()
  const hostRef = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<Point | null>(null)
  const [failed, setFailed] = useState(false)
  const headingId = useId()
  const summaryId = useId()

  useEffect(() => {
    const host = hostRef.current
    if (!host || !series) return
    let disposed = false
    let remove: (() => void) | undefined
    setHover(null)

    import('lightweight-charts')
      .then((lc) => {
        if (disposed) return
        const ink = cssVar('--ink', '#151e19')
        const loss = cssVar('--loss', '#a8241b')
        const muted = cssVar('--muted', '#4a5a50')
        const rule = cssVar('--rule', '#c9d5cb')
        const ruleStrong = cssVar('--rule-strong', '#768e7e')
        const paper = cssVar('--paper', '#f1f4ee')
        const font = cssVar('--font', 'system-ui, sans-serif')
        const whole = (p: number) => money(p, { whole: true })
        const byLabel = new Map(series.points.map((p) => [String(p.time), p]))

        const chart = lc.createChart(host, {
          autoSize: true,
          layout: {
            background: { type: lc.ColorType.Solid, color: paper },
            textColor: muted,
            fontFamily: font,
            fontSize: 12,
            attributionLogo: true,
          },
          grid: { vertLines: { visible: false }, horzLines: { color: withAlpha(rule, 0.9) } },
          // entireTextOnly: no price label half cut off at the top or bottom edge.
          rightPriceScale: { borderVisible: false, entireTextOnly: true, scaleMargins: { top: 0.12, bottom: 0.08 } },
          timeScale: {
            borderVisible: false,
            fixLeftEdge: true,
            fixRightEdge: true,
            timeVisible: false,
            secondsVisible: false,
          },
          // A page to read, not a terminal: no drag or wheel zoom, so the chart
          // never traps a scroll on a phone.
          handleScroll: false,
          handleScale: false,
          crosshair: {
            mode: lc.CrosshairMode.Magnet,
            vertLine: { color: ruleStrong, labelBackgroundColor: ink },
            horzLine: { color: ruleStrong, labelBackgroundColor: ink },
          },
          localization: {
            locale: 'en-US',
            priceFormatter: whole,
            timeFormatter: (time: unknown) => byLabel.get(timeKey(time))?.label ?? '',
          },
        })

        const line = chart.addSeries(lc.BaselineSeries, {
          baseValue: { type: 'price', price: series.base },
          topLineColor: ink,
          topFillColor1: withAlpha(ink, 0.16),
          topFillColor2: withAlpha(ink, 0.03),
          bottomLineColor: loss,
          bottomFillColor1: withAlpha(loss, 0.03),
          bottomFillColor2: withAlpha(loss, 0.2),
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: true,
          priceFormat: { type: 'custom', formatter: whole, minMove: 0.01 },
        })
        line.setData(series.points.map((p) => ({ time: p.time as never, value: p.value })))
        line.createPriceLine({
          price: series.base,
          color: ruleStrong,
          lineWidth: 1,
          lineStyle: lc.LineStyle.Dashed,
          axisLabelVisible: false,
          title: series.baseLabel,
        })
        chart.timeScale().fitContent()

        chart.subscribeCrosshairMove((param) => {
          if (param.time === undefined || !param.point) {
            setHover(null)
            return
          }
          setHover(byLabel.get(timeKey(param.time)) ?? null)
        })
        remove = () => chart.remove()
      })
      .catch(() => {
        if (!disposed) setFailed(true)
      })

    return () => {
      disposed = true
      remove?.()
    }
  }, [series, theme])

  if (!series) {
    return (
      <section className="section" aria-labelledby={headingId}>
        <h2 id={headingId}>Equity</h2>
        <p className="muted">No equity history has been published yet.</p>
      </section>
    )
  }

  const shown = hover ?? series.points.at(-1)!
  const diff = shown.value - series.base
  const diffPct = series.base ? diff / series.base : null

  return (
    <section className="section" aria-labelledby={headingId}>
      <div className="chart-head">
        <h2 id={headingId}>Equity</h2>
      </div>
      <figure className="m-0">
        <p className="chart-readout num small" aria-hidden="true">
          <span className="muted">{shown.label}</span> <strong>{money(shown.value)}</strong>{' '}
          <span className={tone(diff)}>
            {signedMoney(diff)} ({signedPct(diffPct)}) against {series.baseLabel.toLowerCase()}
          </span>
        </p>
        <div ref={hostRef} className="chart-box" role="img" aria-labelledby={summaryId} data-testid="equity-chart">
          {failed ? <p className="muted small p-4">The chart could not be drawn; the table below has every value.</p> : null}
        </div>
        <figcaption id={summaryId} className="small muted mt-3 prose">
          {series.summary}
        </figcaption>
      </figure>
      <DataTable series={series} />
    </section>
  )
}
