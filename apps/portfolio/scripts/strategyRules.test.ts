/**
 * The Strategy page explains the trading rules in words
 * (src/pages/StrategyPage.tsx, tested in src/pages/StrategyPage.test.tsx).
 * This pins the code those words describe, so a change to the rules fails
 * here until the page is re-worded to match.
 */
import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const repo = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../..')
const read = (path: string) => readFileSync(join(repo, path), 'utf8')

describe('backtest/macd_sma.py', () => {
  const source = read('backtest/macd_sma.py')
  const rule = (name: string) => new RegExp(`^\\s+${name} = (.+)$`, 'm').exec(source)?.[1]?.trim()

  it('buys and sells on the four conditions the Strategy page names', () => {
    expect(rule('buy_signal')).toBe('valid & hist_co & (macd > 0) & (fast_ma > slow_ma) & (close_lag > veryslow_ma)')
    expect(rule('sell_signal')).toBe('valid & hist_cu & (macd < 0) & (fast_ma < slow_ma) & (close_lag < veryslow_ma)')
  })

  it('lags the trend filter’s close by the slow length', () => {
    expect(source).toMatch(/^\s+close_lag\[slow_len:\] = close\[:-slow_len\]$/m)
  })
})
