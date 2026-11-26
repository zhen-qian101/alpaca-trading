/**
 * The shape of /data/portfolio.json, mirrored from
 * apps/portfolio/schema/portfolio.schema.json (the exporter's leak gate).
 *
 * Keep the two in step: src/data/schemaSync.test.ts compares every property,
 * bound, pattern and enum here against the JSON schema and fails on any
 * difference. Conventions carried over from the schema: every *_pct,
 * win_rate, stop_pct, deploy, suggested_size and daily_loss_limit is a
 * FRACTION (0.0123 = 1.23%); money is USD; timestamps are UTC
 * (YYYY-MM-DDTHH:MM:SSZ); dates are US/Eastern session dates.
 *
 * The account is DAILY equity only (owner decision): there is no intraday
 * series, and a file that carries one is refused like any undeclared field.
 * Its equity figures stop at the last completed session: the current equity,
 * with a position's qty and entry price, would give back a live price.
 */
import {
  arr,
  bool,
  int,
  nullable,
  num,
  obj,
  oneOf,
  optional,
  str,
  tagged,
  validate,
  type Infer,
  type Validation,
} from './schemaKit.ts'

const NO_CONTROL_CHARS = '^[^\\u0000-\\u001F\\u007F]*$'

const date = str({ pattern: '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' })
const timestamp = str({ pattern: '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$' })
const symbol = str({ maxLength: 31, pattern: '^[A-Z0-9]{1,15}([/.-][A-Z0-9]{1,15})?$' })
const note = str({ maxLength: 300, pattern: NO_CONTROL_CHARS })
const money = num({ minimum: -1000000000, maximum: 1000000000 })
const fillPrice = num({ exclusiveMinimum: 0, maximum: 10000000 })
const quantity = num({ exclusiveMinimum: 0, maximum: 1000000000 })
const ratio = num({ minimum: -1000, maximum: 1000 })
const unitFraction = num({ minimum: 0, maximum: 1 })
const strategyParam = num({ minimum: 0, maximum: 100000 })

const dayReturn = obj({ date, return_pct: ratio })

const account = obj({
  paper: bool(),
  start_date: nullable(date),
  start_equity: nullable(money),
  equity: money,
  day_pl_pct: nullable(ratio),
  total_return_pct: nullable(ratio),
  max_drawdown_pct: nullable(num({ minimum: -1, maximum: 0 })),
  best_day: nullable(dayReturn),
  worst_day: nullable(dayReturn),
  daily: arr(obj({ date, equity: money }), 5000),
})

/**
 * No unrealized P&L or distance to the stop: each would give back the broker's
 * current price. stop_pct is set only where the site can vouch for a stop (the
 * exporter's armed_stop); null means it cannot, not that none exists.
 */
const position = obj({
  symbol,
  side: oneOf('long', 'short'),
  qty: quantity,
  avg_entry_price: fillPrice,
  stop_pct: nullable(unitFraction),
})

const option = obj({
  underlying: symbol,
  type: oneOf('put', 'call'),
  side: oneOf('short'),
  strike: nullable(fillPrice),
  expiry: nullable(date),
  qty: quantity,
  premium_received: nullable(money),
})

const roundTrip = obj({
  symbol,
  side: oneOf('long'),
  qty: quantity,
  entry_price: fillPrice,
  exit_price: fillPrice,
  entry_at: timestamp,
  exit_at: timestamp,
  pnl: money,
  pnl_pct: ratio,
  strategy: nullable(oneOf('ema', 'bollinger_rsi', 'macd_sma')),
})

const trades = obj({
  stats: obj({
    round_trips: int({ minimum: 0, maximum: 1000000 }),
    wins: int({ minimum: 0, maximum: 1000000 }),
    losses: int({ minimum: 0, maximum: 1000000 }),
    win_rate: nullable(unitFraction),
    avg_win: nullable(money),
    avg_loss: nullable(money),
    avg_win_pct: nullable(ratio),
    avg_loss_pct: nullable(ratio),
    profit_factor: nullable(num({ minimum: 0, maximum: 1000000 })),
    realized_pnl: money,
  }),
  round_trips: arr(roundTrip, 500),
})

const contingency = obj({
  condition: oneOf('close_above', 'close_below'),
  level: num({ exclusiveMinimum: 0, maximum: 10000000 }),
  valid_until: timestamp,
})

const signals = obj({
  date: nullable(date),
  rows: arr(
    obj({
      symbol,
      bias: nullable(oneOf('long', 'flat', 'short')),
      priority: nullable(int({ minimum: -1000, maximum: 1000 })),
      suggested_size: nullable(unitFraction),
      contingencies: arr(contingency, 10),
    }),
    100,
  ),
})

/** The strategy types the engine runs: backtest/multi_strategy.py STRATEGY_RUNNERS. */
const strategySpec = tagged('type', {
  ema: obj({
    type: oneOf('ema'),
    fast: optional(strategyParam),
    slow: optional(strategyParam),
    window: optional(strategyParam),
  }),
  bollinger_rsi: obj({
    type: oneOf('bollinger_rsi'),
    rsi_length: optional(strategyParam),
    bb_length: optional(strategyParam),
    bb_mult: optional(strategyParam),
  }),
  macd_sma: obj({
    type: oneOf('macd_sma'),
    fast_len: optional(strategyParam),
    slow_len: optional(strategyParam),
    signal_len: optional(strategyParam),
    veryslow_len: optional(strategyParam),
  }),
})

const strategy = obj({
  symbols: arr(
    obj({
      symbol,
      enabled: bool(),
      on_watchlist: nullable(bool()),
      interval: nullable(str({ pattern: '^[0-9]{1,3}(m|h|d|wk|mo)$' })),
      stop_pct: nullable(unitFraction),
      deploy: nullable(unitFraction),
      strategies: arr(strategySpec, 10),
    }),
    100,
  ),
  risk: obj({
    daily_loss_limit: nullable(unitFraction),
    daily_loss_hard_cap: unitFraction,
    max_position_shares_cap: num({ minimum: 0, maximum: 1000000000 }),
    allow_extended_hours: bool(),
    sleeve_reserve_pct: unitFraction,
  }),
})

// A language model writes each video's title, so none is published: the
// page labels a recap by its date.
const recap = obj({
  date,
  youtube_id: str({ pattern: '^[A-Za-z0-9_-]{11}$' }),
  duration_s: nullable(num({ minimum: 0, maximum: 36000 })),
})

export const SECTIONS = ['account', 'positions', 'options', 'trades', 'signals', 'strategy', 'recaps'] as const

export const portfolioSchema = obj({
  meta: obj({
    schema: oneOf(1),
    as_of: timestamp,
    data_delay: note,
    generated_by: str({
      maxLength: 80,
      pattern: '^public_export/[0-9]{1,4}\\.[0-9]{1,4}\\.[0-9]{1,4}(\\+g[0-9a-f]{4,40})?$',
    }),
    unavailable: arr(oneOf(...SECTIONS), 7, { uniqueItems: true }),
  }),
  account: nullable(account),
  positions: obj({
    equities: nullable(arr(position, 100)),
    options: nullable(arr(option, 20)),
  }),
  trades: nullable(trades),
  signals: nullable(signals),
  strategy: nullable(strategy),
  recaps: nullable(arr(recap, 12)),
})

export type Portfolio = Infer<typeof portfolioSchema>
export type Account = NonNullable<Portfolio['account']>
export type Position = Infer<typeof position>
export type OptionPosition = Infer<typeof option>
export type Trades = NonNullable<Portfolio['trades']>
export type RoundTrip = Infer<typeof roundTrip>
export type Signals = NonNullable<Portfolio['signals']>
export type SignalRow = Signals['rows'][number]
export type Contingency = Infer<typeof contingency>
export type Strategy = NonNullable<Portfolio['strategy']>
export type SymbolStrategy = Strategy['symbols'][number]
export type StrategySpec = Infer<typeof strategySpec>
export type StrategyType = StrategySpec['type']
export type Risk = Strategy['risk']
export type Recap = Infer<typeof recap>

/** Validate a parsed portfolio.json. Extra fields anywhere are refused. */
export function parsePortfolio(value: unknown): Validation<Portfolio> {
  return validate<typeof portfolioSchema>(portfolioSchema, value)
}
