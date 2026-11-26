/**
 * Stand-in for lightweight-charts (jsdom has no canvas). It records what the
 * page asked it to draw, so tests can check the chart gets the account's own
 * equity and keeps the attribution logo on.
 */
export const chartCalls = {
  created: 0,
  removed: 0,
  options: [] as Record<string, unknown>[],
  series: [] as { kind: unknown; options: Record<string, unknown> }[],
  data: [] as unknown[][],
  priceLines: [] as Record<string, unknown>[],
}

export function resetChartCalls(): void {
  chartCalls.created = 0
  chartCalls.removed = 0
  chartCalls.options = []
  chartCalls.series = []
  chartCalls.data = []
  chartCalls.priceLines = []
}

export const chartModule = {
  ColorType: { Solid: 'solid' },
  CrosshairMode: { Magnet: 1 },
  LineStyle: { Dashed: 2 },
  BaselineSeries: { type: 'Baseline' },
  createChart: (_host: HTMLElement, options: Record<string, unknown>) => {
    chartCalls.created++
    chartCalls.options.push(options)
    return {
      addSeries: (kind: unknown, seriesOptions: Record<string, unknown>) => {
        chartCalls.series.push({ kind, options: seriesOptions })
        return {
          setData: (d: unknown[]) => chartCalls.data.push(d),
          createPriceLine: (o: Record<string, unknown>) => {
            chartCalls.priceLines.push(o)
            return {}
          },
        }
      },
      timeScale: () => ({ fitContent: () => {} }),
      subscribeCrosshairMove: () => {},
      remove: () => {
        chartCalls.removed++
      },
    }
  },
}
