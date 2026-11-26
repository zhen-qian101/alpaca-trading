import { useId, type ReactNode } from 'react'

import { Unavailable } from '../components/Holdings.tsx'
import { Page } from '../components/Layout.tsx'
import { usePortfolio } from '../data/portfolioContext.ts'
import type { Risk, Strategy, StrategySpec, StrategyType, SymbolStrategy } from '../data/portfolio.ts'
import { param, pct, pctShort, quantity, signedPct } from '../lib/format.ts'
import { shareOfEquity, worstTrip } from '../lib/stats.ts'
import { STRATEGY_LABEL } from '../lib/strategies.ts'
import { LedgerTable } from '../components/LedgerTable.tsx'

const ORDER: StrategyType[] = ['macd_sma', 'bollinger_rsi', 'ema']

/** The parameters of one strategy, in a fixed order with their meaning. */
function paramList(spec: StrategySpec): { name: string; value: number | undefined }[] {
  switch (spec.type) {
    case 'macd_sma':
      return [
        { name: 'fast', value: spec.fast_len },
        { name: 'slow', value: spec.slow_len },
        { name: 'signal', value: spec.signal_len },
        { name: 'trend', value: spec.veryslow_len },
      ]
    case 'bollinger_rsi':
      return [
        { name: 'RSI', value: spec.rsi_length },
        { name: 'band', value: spec.bb_length },
        { name: 'width', value: spec.bb_mult },
      ]
    case 'ema':
      return [
        { name: 'fast', value: spec.fast },
        { name: 'slow', value: spec.slow },
        { name: 'window', value: spec.window },
      ]
  }
}

const EXPLAIN: Record<StrategyType, { body: ReactNode; params: string }> = {
  macd_sma: {
    body: (
      <>
        <p>
          A trend follower, adapted from ChartArt’s public “MACD + SMA 200” script. Its MACD is built from simple
          moving averages: the fast average minus the slow one, with a signal line averaged over that.
        </p>
        <p>
          It buys when the MACD histogram turns positive while the MACD is above zero and the fast average is above the
          slow one, and only if the close a slow-length ago was above the long trend average. It sells on the mirror
          image: when the histogram turns negative while the MACD is below zero and the fast average is under the slow
          one, and only if the close a slow-length ago was below the long trend average.
        </p>
      </>
    ),
    params: 'Fast, slow and signal lengths, and the trend average length, all in bars.',
  },
  bollinger_rsi: {
    body: (
      <>
        <p>
          A rebound trade, adapted from ChartArt’s public “RSI + Bollinger Bands” script. It buys when, on the same
          bar, RSI crosses up through 50 and the price closes back above the lower Bollinger band: momentum turning up
          just as price recovers from a stretch.
        </p>
        <p>It sells on the mirror image: RSI crossing down through 50 as the price falls back below the upper band.</p>
      </>
    ),
    params: 'RSI length and band length in bars; band width in standard deviations.',
  },
  ema: {
    body: (
      <p>
        It buys when the fast exponential moving average crosses above the slow one. It sells when they cross back,
        when the close reaches the highest close of the window before entry, or when it falls below that window’s
        lowest close.
      </p>
    ),
    params: 'Fast and slow average lengths and the look-back window, in bars.',
  },
}

function Steps() {
  const headingId = useId()
  const steps = [
    {
      title: 'A bar closes',
      body: 'Every five minutes the engine pulls the latest bars for each enabled symbol and keeps only closed ones. It never acts on a bar that is still forming.',
    },
    {
      title: 'The strategies run again',
      body: 'It re-runs each symbol’s strategies over a rolling window of bars, using the same code the backtests use, and compares the result with the last bar it handled. Only a signal on a new bar counts.',
    },
    {
      title: 'The risk gate checks the order',
      body: 'A buy is refused when research keeps a watchlist and the symbol is not on it, crypto included. A share buy is also refused when research has marked the symbol flat or short, unless a standing order covers the price; crypto is exempt from the bias. A share buy must fall inside the hours it may trade, no buy may exceed the size cap, and no buy goes out while the account is down by its daily loss limit. A sell is held back only when no session is open to take it, or when the same order went out less than a minute before.',
    },
    {
      title: 'Rotation keeps one position',
      body: 'The strategies hold one position at a time. A buy for a different symbol replaces the current holding only if research ranks it higher; otherwise it is refused, so the book does not churn. Shares the options sleeve was assigned are outside the rotation: they can be held alongside that position, and a rotation never sells them.',
    },
    {
      title: 'The order goes to Alpaca’s paper API',
      body: 'A buy is sized at the symbol’s deploy share of the share book: equity less the cash the options sleeve keeps back. It goes out as a market order in regular hours, a marketable limit order outside them. Every decision, filled or refused, is written to a journal.',
    },
    {
      title: 'The position is sold whole',
      body: 'When a strategy’s exit fires, or a bar closes through its stop, the whole position in that symbol is sold at the next available price. A check at the end of every poll closes any position the strategies have already left, in case an exit order failed.',
    },
  ]
  return (
    <section className="section" aria-labelledby={headingId}>
      <h2 id={headingId}>How a trade happens</h2>
      <ol className="steps">
        {steps.map((s) => (
          <li key={s.title}>
            <h3>{s.title}</h3>
            <p>{s.body}</p>
          </li>
        ))}
      </ol>
    </section>
  )
}

function Strategies({ strategy }: { strategy: Strategy | null }) {
  const headingId = useId()
  const inUse = new Set(strategy?.symbols.flatMap((s) => s.strategies.map((x) => x.type)) ?? [])
  const types = strategy ? ORDER.filter((t) => inUse.has(t)) : ORDER
  return (
    <section className="section" aria-labelledby={headingId}>
      <h2 id={headingId}>The strategies</h2>
      <div className="prose">
        <p>
          Each symbol runs one or more strategies. When it runs two, whichever buy signal fires first takes the
          position, and an exit from either one closes it. Every decision is made on a closed bar.
        </p>
        <p>
          Every strategy also carries the symbol’s stop. Each strategy keeps its own simulated trade, entered at the
          close of the bar its buy signal fired on. While that trade is open, a bar that closes at or below its entry
          less the stop percentage sells the whole position in the symbol at the next available price. The stop is
          checked on closed bars, not held as an order at the broker, so after a gap or a fast bar the sale can land
          well past it.
        </p>
        <p>
          The stop follows the simulated trade, not the account’s fill, and lasts only as long as that trade. If a
          strategy’s simulation enters and stops out on the same bar, the engine handles the stop before the buy, so
          the position that buy opens can be left with no stop at all. The original scripts also sell short; this
          book is long only, so their short signal means sell and wait.
        </p>
      </div>
      <dl className="defs">
        {types.map((type) => (
          <div key={type}>
            <dt>{STRATEGY_LABEL[type]}</dt>
            <dd>
              {EXPLAIN[type].body}
              <p className="params">{EXPLAIN[type].params}</p>
            </dd>
          </div>
        ))}
      </dl>
      {strategy && inUse.size === 0 ? <p className="muted">No symbol has a strategy configured.</p> : null}
    </section>
  )
}

function specText(spec: StrategySpec): string {
  const values = paramList(spec)
    .filter((p) => p.value !== undefined)
    .map((p) => `${p.name} ${param(p.value)}`)
  return values.length ? `${STRATEGY_LABEL[spec.type]} (${values.join(', ')})` : STRATEGY_LABEL[spec.type]
}

function status(s: SymbolStrategy): { text: string; muted: boolean } {
  if (!s.enabled) return { text: 'Paused', muted: true }
  // The gate refuses every buy for a symbol research has not listed, crypto included.
  if (s.on_watchlist === false) return { text: 'Not on research’s watchlist', muted: true }
  return { text: 'Trading', muted: false }
}

/** "97%", and what that is of the whole account once the sleeve's reserve is set aside. */
function Deploy({ deploy, reserve, inline = false }: { deploy: number | null; reserve: number; inline?: boolean }) {
  const whole = shareOfEquity(deploy, reserve)
  if (reserve <= 0 || whole === null) return <>{pctShort(deploy)}</>
  return inline ? (
    <>
      {pctShort(deploy)} <span className="muted">({pct(whole, 0)} of equity)</span>
    </>
  ) : (
    <>
      {pctShort(deploy)}
      <span className="block small muted">{pct(whole, 0)} of equity</span>
    </>
  )
}

function deployNote(symbols: readonly SymbolStrategy[], reserve: number): string {
  const base = 'The engine’s settings for each symbol.'
  const tail =
    ' A paused symbol is not polled at all. One that is not on research’s watchlist is polled, but the risk gate refuses its buys.'
  if (reserve <= 0) return `${base} Deploy is the share of equity a buy uses.${tail}`
  const deploys = symbols.map((s) => s.deploy).filter((d): d is number => d !== null)
  const top = deploys.length ? Math.max(...deploys) : null
  const example = top !== null ? `, so ${pctShort(top)} is about ${pct(shareOfEquity(top, reserve), 0)} of equity` : ''
  return (
    `${base} Deploy is the share of the share book a buy uses. The share book is equity less the ` +
    `${pctShort(reserve)} the options sleeve keeps back${example}.${tail}`
  )
}

function SymbolTable({ symbols, reserve }: { symbols: readonly SymbolStrategy[]; reserve: number }) {
  const headingId = useId()
  const rows = [...symbols].sort((a, b) => Number(b.enabled) - Number(a.enabled) || a.symbol.localeCompare(b.symbol))
  const note = deployNote(rows, reserve)
  return (
    <section className="section" aria-labelledby={headingId}>
      <h2 id={headingId}>Per symbol</h2>
      {rows.length === 0 ? (
        <p>No symbols are configured.</p>
      ) : (
        <>
          {/* Below 40rem the six columns would scroll sideways and cut the
              strategy names off; the list after this shows the same settings
              there instead. Only one copy is displayed at any width, so only
              one reaches assistive tech. */}
          <div className="max-sm:hidden">
            <LedgerTable label="Per-symbol settings" note={note}>
              <thead>
                <tr>
                  <th scope="col">Symbol</th>
                  <th scope="col">Status</th>
                  <th scope="col">Bars</th>
                  <th scope="col" className="col">
                    Strategies and parameters
                  </th>
                  <th scope="col" className="n col">
                    Stop
                  </th>
                  <th scope="col" className="n">
                    Deploy
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((s) => {
                  const st = status(s)
                  return (
                    <tr key={s.symbol}>
                      <th scope="row">{s.symbol}</th>
                      <td className={st.muted ? 'muted' : ''}>{st.text}</td>
                      <td>{s.interval ?? '–'}</td>
                      <td className="col wrap-cell">
                        {s.strategies.length ? (
                          s.strategies.map((spec, i) => (
                            <span key={i} className="block">
                              {specText(spec)}
                            </span>
                          ))
                        ) : (
                          <span className="muted">None</span>
                        )}
                      </td>
                      <td className="n col">{pctShort(s.stop_pct)}</td>
                      <td className="n">
                        <Deploy deploy={s.deploy} reserve={reserve} />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </LedgerTable>
          </div>
          <div className="sm:hidden" data-testid="symbols-narrow">
            <p className="table-note">{note}</p>
            <ul className="symbol-list" aria-label="Per-symbol settings">
              {rows.map((s) => {
                const st = status(s)
                return (
                  <li key={s.symbol}>
                    <p className="symbol-list__head">
                      <strong>{s.symbol}</strong> <span className={st.muted ? 'muted' : ''}>{st.text}</span>
                    </p>
                    {s.strategies.length ? (
                      <ul className="symbol-list__strategies">
                        {s.strategies.map((spec, i) => (
                          <li key={i}>{specText(spec)}</li>
                        ))}
                      </ul>
                    ) : (
                      <p className="muted">No strategy</p>
                    )}
                    <dl className="symbol-list__facts">
                      <div>
                        <dt>Bars</dt>
                        <dd>{s.interval ?? '–'}</dd>
                      </div>
                      <div>
                        <dt>Stop</dt>
                        <dd>{pctShort(s.stop_pct)}</dd>
                      </div>
                      <div>
                        <dt>Deploy</dt>
                        <dd>
                          <Deploy deploy={s.deploy} reserve={reserve} inline />
                        </dd>
                      </div>
                    </dl>
                  </li>
                )
              })}
            </ul>
          </div>
        </>
      )}
    </section>
  )
}

function stopRange(symbols: readonly SymbolStrategy[]): string | null {
  const stops = symbols.filter((s) => s.enabled && s.stop_pct !== null).map((s) => s.stop_pct as number)
  if (!stops.length) return null
  const lo = Math.min(...stops)
  const hi = Math.max(...stops)
  return lo === hi ? `${pctShort(lo)} below entry` : `${pctShort(lo)} to ${pctShort(hi)} below entry, set per symbol`
}

/**
 * The limits as relay/risk_filter.py check() applies them, and nothing it
 * does not: one position at a time is relay/order_manager.py's rotation,
 * fixed in code, not a research setting. Shares the put sleeve was assigned
 * are left out of that rotation (protected_symbols), so they can be held
 * alongside it.
 */
function RiskLimits({
  risk,
  symbols,
  worst,
}: {
  risk: Risk
  symbols: readonly SymbolStrategy[]
  worst: number | null
}) {
  const headingId = useId()
  const stops = stopRange(symbols)
  const cap = quantity(risk.max_position_shares_cap)
  return (
    <section className="section" aria-labelledby={headingId}>
      <h2 id={headingId}>Risk limits</h2>
      <dl className="leaders">
        {stops ? (
          <div>
            <dt>Stop on each position</dt>
            <dd>{stops}</dd>
          </div>
        ) : null}
        <div>
          <dt>Daily loss halt</dt>
          <dd>
            {risk.daily_loss_limit === null
              ? '–'
              : `Buys stop at ${pctShort(risk.daily_loss_limit)} below the previous close`}
          </dd>
        </div>
        <div>
          <dt>Positions at once</dt>
          <dd>One from the signals, fixed in code, plus any shares the options sleeve was assigned</dd>
        </div>
        <div>
          <dt>Largest buy order</dt>
          <dd>{cap} shares</dd>
        </div>
        <div>
          <dt>Share buys outside regular hours</dt>
          <dd>{risk.allow_extended_hours ? 'Allowed' : 'Not allowed'}</dd>
        </div>
        <div>
          <dt>Kept back for the options sleeve</dt>
          <dd>{risk.sleeve_reserve_pct > 0 ? `${pctShort(risk.sleeve_reserve_pct)} of equity` : 'None'}</dd>
        </div>
      </dl>
      <div className="prose">
        <p>
          <strong>Stops</strong> are checked, not placed. There is no stop order at the broker: when one of the
          symbol’s bars closes at or below the level, measured from the strategy’s simulated entry, the engine sells at
          the next available price. A gap between sessions or a fast bar can take the sale well past the stop, so a
          stop is not a cap on the loss.
          {worst !== null ? ` The worst closed round trip so far returned ${signedPct(worst)}.` : ''}
        </p>
        <p>
          <strong>The daily loss halt</strong> is checked before every buy, crypto included: while equity is down by
          the limit or more against the previous close, the buy is refused, and once equity recovers above it buys go
          out again. It never holds back a sell, so it cannot trap a position.
        </p>
        <p>
          <strong>Hours.</strong> A share buy goes out in the regular session, 9:30 a.m. to 4 p.m. New York time on
          weekdays, or, when research allows extended hours, at any time from Sunday 8 p.m. to Friday 8 p.m. A share
          sale may go out at any time in that wider window whatever research says; only the weekend stops it. Crypto
          trades around the clock and is exempt from both.
        </p>
        <p>
          <strong>Ceilings.</strong> Research sets the daily loss limit and may give a symbol a lower size cap, but
          code caps both: a halt at {pctShort(risk.daily_loss_hard_cap)} at most and no buy over {cap} shares. A
          value over a ceiling is clamped to it. One position at a time is the engine’s own rule, not research’s: a
          buy in another symbol replaces the holding only when research ranks that symbol higher. Shares the options
          sleeve was assigned sit outside that rule: the rotation never sells them, so they can be held alongside the
          engine’s position until the sleeve sells them.
        </p>
      </div>
    </section>
  )
}

function Research() {
  const headingId = useId()
  return (
    <section className="section" aria-labelledby={headingId}>
      <h2 id={headingId}>Research: which symbols, in what order</h2>
      <div className="prose">
        <p>
          Five times each weekday, at 9:15, 10:00, 12:30, 15:00 and 18:00 New York time, a headless Claude Code session
          reads market data and news and writes a decisions file: a watchlist, a bias (long, flat or short) and a
          priority for each symbol. The first run of the day can add and drop symbols; the others re-rank the list.
        </p>
        <p>
          Research never trades. Its data tools have no order access, and re-ranking priorities does not move the
          book: a rotation happens only when a strategy signal fires for the higher-ranked symbol.
        </p>
        <p>
          A flat symbol can carry a standing order: a price level research approves in advance, with an expiry, such
          as “a close above this level until Friday.” When a buy signal arrives and the price meets it, the gate lets
          the buy through without waiting for the next research run. The check is mechanical; no model is asked at
          signal time.
        </p>
        <p>
          Crypto trades around the clock, including weekends when research does not run, so it is exempt from the
          bias, though not from the watchlist: a crypto buy still needs research to list the coin, but a flat bias does
          not block it.
        </p>
      </div>
    </section>
  )
}

function OptionsSleeve() {
  const headingId = useId()
  return (
    <section className="section" aria-labelledby={headingId}>
      <h2 id={headingId}>Options sleeve</h2>
      <p className="prose">
        Apart from the share book, a small sleeve sells one cash-secured put a week, a fixed distance below the market,
        paid for from its own cash reserve: a share of equity the share book never sizes a buy against. If a put is
        assigned, the shares are held alongside the share book’s own position, with no stop, and sold at the sleeve’s
        next weekly run. These trades are not part of the round-trip figures.
      </p>
    </section>
  )
}

export function StrategyPage() {
  const state = usePortfolio()
  const strategy = state.status === 'ready' ? state.data.strategy : null
  const trades = state.status === 'ready' ? state.data.trades : null
  return (
    <Page
      title="Strategy & risk"
      heading="Strategy & risk"
      intro={
        <p className="lede">
          Two layers decide every trade. Mechanical strategies, run on each symbol’s hourly bars, decide when to buy
          and sell. A research layer decides which symbols they may buy at all. Between both and the broker sits a risk
          gate in code, and it has the final word.
        </p>
      }
    >
      <Steps />
      <Strategies strategy={strategy} />
      {state.status === 'ready' && !strategy ? <Unavailable what="strategy settings" /> : null}
      {strategy ? <SymbolTable symbols={strategy.symbols} reserve={strategy.risk.sleeve_reserve_pct} /> : null}
      {strategy ? <RiskLimits risk={strategy.risk} symbols={strategy.symbols} worst={worstTrip(trades)} /> : null}
      <Research />
      <OptionsSleeve />
    </Page>
  )
}
