import { useState } from 'react'

import { EquityChart } from '../components/EquityChart.tsx'
import { Positions, SignalBoard, Unavailable } from '../components/Holdings.tsx'
import { Page } from '../components/Layout.tsx'
import { CADENCE, DISCLAIMER, LOADING } from '../lib/copy.ts'
import { usePortfolio } from '../data/portfolioContext.ts'
import type { Portfolio } from '../data/portfolio.ts'
import { etDateTime, longDate, money, pct, signedPct, tone } from '../lib/format.ts'
import { Link } from '../lib/router.tsx'
import { daysLive } from '../lib/stats.ts'
import { site } from '../site.ts'

const SECTION_NAMES: Record<string, string> = {
  account: 'account',
  positions: 'positions',
  options: 'options sleeve',
  trades: 'trades',
  signals: 'research board',
  strategy: 'strategy settings',
  recaps: 'recaps',
}

/**
 * The headline: where the account started and what it was worth after the last
 * completed session, as two sentences. The file carries no equity from a
 * session still in progress: that, with a position's size and entry price,
 * would give back the symbol's live price.
 */
function hero(data: Portfolio | null): { first: string; second: string | null } {
  const account = data?.account
  if (!account) return { first: 'A self-hosted trading system, trading paper money and keeping its own books.', second: null }
  const last = account.daily.at(-1)
  const now = last
    ? `After the session of ${longDate(last.date)}, it was worth ${money(account.equity)}.`
    : `It was last worth ${money(account.equity)}.`
  if (account.start_date && account.start_equity !== null)
    return {
      first: `Since ${longDate(account.start_date)}, this system has traded ${money(account.start_equity, { whole: true })} of paper money.`,
      second: now,
    }
  return { first: 'This system trades a paper account.', second: now }
}

function Totals({ data }: { data: Portfolio }) {
  const account = data.account
  const stats = data.trades?.stats ?? null
  const days = account ? daysLive(account.start_date, data.meta.as_of) : null
  const items = [
    {
      label: 'Total return',
      value: signedPct(account?.total_return_pct),
      tone: tone(account?.total_return_pct),
    },
    {
      label: 'Max drawdown',
      value: pct(account?.max_drawdown_pct),
      tone: account?.max_drawdown_pct ? 'loss' : 'even',
    },
    { label: 'Win rate', value: pct(stats?.win_rate, 0), tone: '' },
    { label: 'Round trips', value: stats ? String(stats.round_trips) : '–', tone: '' },
    { label: 'Days live', value: days === null ? '–' : String(days), tone: '' },
  ]
  return (
    <section aria-label="Headline figures">
      <dl className="totals">
        {items.map((item) => (
          <div key={item.label}>
            <dt>{item.label}</dt>
            <dd className={item.tone}>{item.value}</dd>
          </div>
        ))}
      </dl>
      <p className="small muted mt-4 prose">
        Total return and drawdown are measured on the equity at the end of each session since the start, up to the
        last completed session: no equity figure on this site moves during a session. Win rate and round trips count
        closed trades, entry to full exit.
        {account?.daily.length ? ` ${account.daily.length} sessions on the equity curve.` : ''}
        {account?.best_day && account.worst_day
          ? ` Best session ${signedPct(account.best_day.return_pct)} on ${longDate(account.best_day.date)}; worst ${signedPct(account.worst_day.return_pct)} on ${longDate(account.worst_day.date)}. Each session is measured against the one before it; a session missing from the broker’s history is skipped, not folded into the next.`
          : ''}
        {account?.day_pl_pct != null ? ` Last completed session ${signedPct(account.day_pl_pct)}.` : ''}
      </p>
    </section>
  )
}

export function OverviewPage() {
  const state = usePortfolio()
  const data = state.status === 'ready' ? state.data : null
  // Read the clock once per visit: the figures are a static file.
  const [now] = useState(() => Date.now())
  const unavailable = data?.meta.unavailable ?? []
  const headline = hero(data)

  return (
    <Page
      title={site.title}
      heading={
        <>
          {headline.first}
          {headline.second ? (
            <>
              {' '}
              <span className="block">{headline.second}</span>
            </>
          ) : null}
        </>
      }
      className="hero"
      intro={
        <>
          <p className="lede">
            A program on a home computer trades a simulated brokerage account through Alpaca’s paper-trading API.
            Rule-based strategies decide when to buy and sell on hourly bars, and a Claude research job decides,
            several times a day, which symbols they may buy. Each weekday evening a local GPU renders a short,
            AI-generated video recap of the day (see Recaps).
          </p>
          <div className="notice">
            {data ? (
              <p className="small">
                <strong>As of {etDateTime(data.meta.as_of)}.</strong>
              </p>
            ) : null}
            <p className="small">{DISCLAIMER}</p>
            <p className="small">{CADENCE}</p>
          </div>
          {unavailable.length ? (
            <div className="notice notice--loss">
              <p className="small">
                Not reported in this export: {unavailable.map((s) => SECTION_NAMES[s] ?? s).join(', ')}.
              </p>
            </div>
          ) : null}
        </>
      }
    >
      {state.status === 'loading' ? (
        <p className="muted">{LOADING}</p>
      ) : null}

      {data ? (
        <>
          {data.account ? <EquityChart account={data.account} /> : <Unavailable what="account" />}
          <Totals data={data} />
          <Positions positions={data.positions} />
            {data.signals ? (
              <SignalBoard
                signals={data.signals}
                nowMs={now}
                sleeveReserve={data.strategy?.risk.sleeve_reserve_pct ?? null}
              />
            ) : (
              <section className="section">
                <h2>Research board</h2>
                <Unavailable what="research board" />
              </section>
            )}
        </>
      ) : null}

      <section className="section prose" aria-label="Further reading">
        <p>
          Every closed trade is on <Link to="/trades">Trades</Link>. The rules, stops and loss limits are on{' '}
          <Link to="/strategy">Strategy &amp; risk</Link>, and the machinery on{' '}
          <Link to="/how-its-built">How it’s built</Link>.
        </p>
      </section>
    </Page>
  )
}
