/**
 * The signal chart: candles with Bollinger bands, an RSI pane and a MACD
 * histogram pane, plus the strategy / account / fill markers, drawn with
 * lightweight-charts v5.
 *
 * Imperative on purpose: React owns WHEN to draw, this owns HOW, and the chart
 * instance outlives data refreshes so the viewer's zoom and pan survive them.
 * Times arrive as TRUE UTC epochs and are labelled in an explicit zone; the
 * date labels also SIT at that zone's midnight (zonedTime.ts).
 */
import {
  CandlestickSeries,
  ColorType,
  createChartEx,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type IHorzScaleBehavior,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'

import type { Candle, SymbolChart } from '../../../contract.ts'
import { ZONES, type ZoneKey } from '../../../lib/time.ts'
import {
  crosshairLabel,
  defaultBarCount,
  defaultRange,
  planVisibleRange,
  themeMarkerColor,
  tickLabel,
  type LogicalRange,
} from './chartMath.ts'
import { zonedTimeScale } from './zonedTime.ts'

export interface ChartPalette {
  panel: string
  text: string
  muted: string
  grid: string
  rule: string
  ruleStrong: string
  up: string
  down: string
  warn: string
  info: string
  fillBuy: string
  fillSell: string
  fontFamily: string
}

/** Read the chart colours from the live CSS tokens, so the chart follows the theme. */
export function readPalette(el: Element): ChartPalette {
  const css = getComputedStyle(el)
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback
  return {
    panel: v('--panel', '#132430'),
    text: v('--ink', '#e4ecf0'),
    muted: v('--muted', '#9db2bf'),
    grid: v('--chart-grid', 'rgba(61,92,112,0.35)'),
    rule: v('--rule', '#2b4556'),
    ruleStrong: v('--rule-strong', '#3d5c70'),
    up: v('--up', '#5ad39b'),
    down: v('--down', '#ff8c8c'),
    warn: v('--warn', '#f3a35c'),
    info: v('--info', '#8fc1ff'),
    fillBuy: v('--fill-buy', '#8fa2ff'),
    fillSell: v('--fill-sell', '#d4a2ff'),
    fontFamily: v('--font-mono', 'ui-monospace, monospace'),
  }
}

export interface CrosshairValues {
  time: number
  candle: Candle | undefined
  rsi: number | undefined
  macd: number | undefined
}

export interface PaneLabel {
  top: number
  text: string
}

interface Options {
  zone: ZoneKey
  palette: ChartPalette
  /** Values under the crosshair, or null when it leaves the chart. */
  onCrosshair: (values: CrosshairValues | null) => void
  /** Where each pane starts, for the HTML pane labels over the canvas. */
  onPanes: (labels: PaneLabel[]) => void
}

interface Drawn {
  chart: IChartApi
  candles: ISeriesApi<'Candlestick'>
  bbUpper: ISeriesApi<'Line'> | null
  bbBasis: ISeriesApi<'Line'> | null
  bbLower: ISeriesApi<'Line'> | null
  rsi: ISeriesApi<'Line'> | null
  macd: ISeriesApi<'Histogram'> | null
  markers: ISeriesMarkersPluginApi<Time>
  paneTexts: string[]
}

const ts = (t: number) => t as UTCTimestamp

export class SignalChart {
  private host: HTMLElement
  private opts: Options
  private drawn: Drawn | null = null
  private symbol: string | null = null
  private shape = ''
  private data: SymbolChart | null = null
  private times: number[] = []
  private byTime = {
    candle: new Map<number, Candle>(),
    rsi: new Map<number, number>(),
    macd: new Map<number, number>(),
  }
  private resize: ResizeObserver | null = null
  private panesFrame: number | null = null

  constructor(host: HTMLElement, opts: Options) {
    this.host = host
    this.opts = opts
    if (typeof ResizeObserver === 'function') {
      this.resize = new ResizeObserver(() => this.schedulePanes())
      this.resize.observe(host)
    }
  }

  /** Draw `data` for `symbol`, keeping the zoom/pan when it is the same symbol. */
  setData(symbol: string, data: SymbolChart): void {
    const shape = [data.bbUpper.length > 0, data.rsi.length > 0, data.macdHist.length > 0].join()
    const sameView = this.drawn !== null && this.symbol === symbol && this.shape === shape
    const prevRange = sameView ? this.drawn!.chart.timeScale().getVisibleLogicalRange() : null
    const prevTimes = this.times

    if (!sameView) {
      this.teardown()
      this.drawn = this.build(data)
      this.symbol = symbol
      this.shape = shape
    }
    this.data = data
    this.fill(data)

    const nextTimes = data.candles.map((c) => c.time)
    this.times = nextTimes
    const planned = planVisibleRange(
      prevRange ? { from: prevRange.from, to: prevRange.to } : null,
      prevTimes,
      nextTimes,
    )
    const range: LogicalRange =
      planned ?? defaultRange(nextTimes.length, defaultBarCount(this.host.clientWidth || 700, nextTimes.length))
    if (nextTimes.length > 0) this.drawn!.chart.timeScale().setVisibleLogicalRange(range)
    this.schedulePanes()
  }

  setZone(zone: ZoneKey): void {
    if (zone === this.opts.zone) return
    this.opts.zone = zone
    // Which bars carry the date labels is worked out in the zone the chart
    // was built with (zonedTime.ts), so a new zone needs a new chart. The
    // view the viewer had is kept.
    const d = this.drawn
    const data = this.data
    const symbol = this.symbol
    if (!d || !data || symbol === null) return
    const range = d.chart.timeScale().getVisibleLogicalRange()
    const times = this.times
    this.teardown()
    this.drawn = this.build(data)
    this.symbol = symbol
    this.times = times
    this.fill(data)
    if (range && times.length > 0) this.drawn.chart.timeScale().setVisibleLogicalRange(range)
    this.schedulePanes()
  }

  setPalette(palette: ChartPalette): void {
    this.opts.palette = palette
    if (!this.drawn) return
    const d = this.drawn
    d.chart.applyOptions(this.chromeOptions())
    d.candles.applyOptions(this.candleOptions())
    d.bbUpper?.applyOptions({ color: palette.info })
    d.bbLower?.applyOptions({ color: palette.info })
    d.bbBasis?.applyOptions({ color: palette.ruleStrong })
    d.rsi?.applyOptions({ color: palette.warn })
    if (this.data) this.fill(this.data)
  }

  /** Values for a bar, for the readout when nothing is hovered. */
  valuesAt(time: number): CrosshairValues {
    return {
      time,
      candle: this.byTime.candle.get(time),
      rsi: this.byTime.rsi.get(time),
      macd: this.byTime.macd.get(time),
    }
  }

  destroy(): void {
    this.resize?.disconnect()
    this.resize = null
    if (this.panesFrame !== null) cancelAnimationFrame(this.panesFrame)
    this.panesFrame = null
    this.teardown()
  }

  // ---------------------------------------------------------------- private

  private teardown(): void {
    if (!this.drawn) return
    this.drawn.chart.unsubscribeCrosshairMove(this.onMove)
    this.drawn.chart.remove()
    this.drawn = null
    this.symbol = null
    this.times = []
  }

  private onMove = (param: MouseEventParams<Time>): void => {
    if (param.time === undefined) {
      this.opts.onCrosshair(null)
      return
    }
    this.opts.onCrosshair(this.valuesAt(param.time as number))
  }

  private zoneOptions() {
    const zone = this.opts.zone
    return {
      localization: { locale: 'en-GB', timeFormatter: (t: Time) => crosshairLabel(t as number, zone) },
      timeScale: { tickMarkFormatter: (t: Time, type: number) => tickLabel(t as number, type, zone) },
    }
  }

  private chromeOptions() {
    const p = this.opts.palette
    return {
      layout: {
        background: { type: ColorType.Solid, color: p.panel },
        textColor: p.muted,
        fontFamily: p.fontFamily,
        fontSize: 11,
        panes: { separatorColor: p.rule, separatorHoverColor: p.ruleStrong, enableResize: false },
      },
      grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
      rightPriceScale: { borderColor: p.rule, minimumWidth: 64 },
      timeScale: { borderColor: p.rule },
      crosshair: {
        vertLine: { color: p.muted, labelBackgroundColor: p.ruleStrong },
        horzLine: { color: p.muted, labelBackgroundColor: p.ruleStrong },
      },
    }
  }

  private candleOptions() {
    const p = this.opts.palette
    return {
      upColor: p.up,
      downColor: p.down,
      borderUpColor: p.up,
      borderDownColor: p.down,
      wickUpColor: p.up,
      wickDownColor: p.down,
    }
  }

  private build(data: SymbolChart): Drawn {
    const p = this.opts.palette
    const chart = createChartEx<Time, IHorzScaleBehavior<Time>>(this.host, zonedTimeScale(ZONES[this.opts.zone].id), {
      autoSize: true,
      ...this.chromeOptions(),
      timeScale: {
        ...this.chromeOptions().timeScale,
        ...this.zoneOptions().timeScale,
        timeVisible: true,
        secondsVisible: false,
        // The refresh logic decides where the view sits; the library must not
        // also shift it when the series is replaced.
        shiftVisibleRangeOnNewBar: false,
      },
      // createChart's own default, which createChartEx does not apply.
      localization: { dateFormat: "dd MMM 'yy", ...this.zoneOptions().localization },
    })

    const candles = chart.addSeries(CandlestickSeries, {
      ...this.candleOptions(),
      priceLineVisible: false,
    })
    // Long-period bands can sit far from price. Keep them out of autoscale so
    // the candles keep the vertical room; a band simply clips where it strays.
    const band = (color: string, style: LineStyle) =>
      chart.addSeries(LineSeries, {
        color,
        lineWidth: 1,
        lineStyle: style,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
        autoscaleInfoProvider: () => null,
      })
    const hasBands = data.bbUpper.length > 0
    const bbUpper = hasBands ? band(p.info, LineStyle.Solid) : null
    const bbBasis = hasBands ? band(p.ruleStrong, LineStyle.Dashed) : null
    const bbLower = hasBands ? band(p.info, LineStyle.Solid) : null

    const paneTexts = [hasBands ? `Price, ${data.bbLabel || 'Bollinger bands'}` : 'Price']
    let pane = 0
    let rsi: ISeriesApi<'Line'> | null = null
    if (data.rsi.length > 0) {
      pane += 1
      rsi = chart.addSeries(LineSeries, { color: p.warn, lineWidth: 2, lastValueVisible: true, priceLineVisible: false }, pane)
      rsi.createPriceLine({ price: 50, color: p.ruleStrong, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: '' })
      paneTexts.push('RSI')
    }
    let macd: ISeriesApi<'Histogram'> | null = null
    if (data.macdHist.length > 0) {
      pane += 1
      macd = chart.addSeries(HistogramSeries, { lastValueVisible: false, priceLineVisible: false }, pane)
      macd.createPriceLine({ price: 0, color: p.ruleStrong, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: '' })
      paneTexts.push(data.macdLabel ? `MACD histogram, ${data.macdLabel}` : 'MACD histogram')
    }
    const panes = chart.panes()
    panes[0]?.setStretchFactor(3)
    for (let i = 1; i < panes.length; i++) panes[i].setStretchFactor(1)

    const markers = createSeriesMarkers(candles, [])
    chart.subscribeCrosshairMove(this.onMove)
    return { chart, candles, bbUpper, bbBasis, bbLower, rsi, macd, markers, paneTexts }
  }

  private fill(data: SymbolChart): void {
    const d = this.drawn
    if (!d) return
    const p = this.opts.palette
    d.candles.setData(data.candles.map((c) => ({ ...c, time: ts(c.time) })))
    d.bbUpper?.setData(data.bbUpper.map((x) => ({ time: ts(x.time), value: x.value })))
    d.bbBasis?.setData(data.bbBasis.map((x) => ({ time: ts(x.time), value: x.value })))
    d.bbLower?.setData(data.bbLower.map((x) => ({ time: ts(x.time), value: x.value })))
    d.rsi?.setData(data.rsi.map((x) => ({ time: ts(x.time), value: x.value })))
    d.macd?.setData(
      data.macdHist.map((x) => ({ time: ts(x.time), value: x.value, color: x.value >= 0 ? p.up : p.down })),
    )
    const markers: SeriesMarker<Time>[] = data.markers.map((m) => ({
      time: ts(m.time),
      position: m.position,
      shape: m.shape,
      text: m.text,
      color: themeMarkerColor(m.color, p),
    }))
    d.markers.setMarkers(markers)

    this.byTime.candle = new Map(data.candles.map((c) => [c.time, c]))
    this.byTime.rsi = new Map(data.rsi.map((x) => [x.time, x.value]))
    this.byTime.macd = new Map(data.macdHist.map((x) => [x.time, x.value]))
  }

  /** Pane heights settle only after the chart's own autosize pass: measure on the next frames. */
  private schedulePanes(): void {
    if (this.panesFrame !== null) cancelAnimationFrame(this.panesFrame)
    this.panesFrame = requestAnimationFrame(() => {
      this.panesFrame = requestAnimationFrame(() => {
        this.panesFrame = null
        this.reportPanes()
      })
    })
  }

  private reportPanes(): void {
    const d = this.drawn
    if (!d) {
      this.opts.onPanes([])
      return
    }
    let top = 0
    const labels: PaneLabel[] = []
    d.chart.panes().forEach((pane, i) => {
      labels.push({ top: top + 4, text: d.paneTexts[i] ?? '' })
      top += pane.getHeight() + 1
    })
    this.opts.onPanes(labels)
  }
}
