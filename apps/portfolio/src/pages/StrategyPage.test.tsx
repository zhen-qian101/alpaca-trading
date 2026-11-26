import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { renderAppAt } from '../test/renderApp.tsx'

describe('strategy explanations', () => {
  // backtest/macd_sma.py _signals(): four conditions each way, the trend
  // filter included on the exit (scripts/strategyRules.test.ts pins the code).
  it('states the MACD + SMA entry and exit with every condition the code checks', async () => {
    await renderAppAt('/strategy')
    const main = screen.getByRole('main')
    expect(main).toHaveTextContent(
      'It buys when the MACD histogram turns positive while the MACD is above zero and the fast average is above ' +
        'the slow one, and only if the close a slow-length ago was above the long trend average.',
    )
    expect(main).toHaveTextContent(
      'It sells on the mirror image: when the histogram turns negative while the MACD is below zero and the fast ' +
        'average is under the slow one, and only if the close a slow-length ago was below the long trend average.',
    )
  })
})
