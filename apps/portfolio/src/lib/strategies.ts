import type { StrategyType } from '../data/portfolio.ts'

export const STRATEGY_LABEL: Record<StrategyType, string> = {
  macd_sma: 'MACD + SMA trend filter',
  bollinger_rsi: 'Bollinger + RSI',
  ema: 'EMA cross',
}
