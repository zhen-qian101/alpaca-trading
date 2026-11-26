/** Navigation and the fixed disclaimer, shared by the layout and the pages. */
export const NAV = [
  { to: '/', label: 'Overview' },
  { to: '/trades', label: 'Trades' },
  { to: '/strategy', label: 'Strategy & risk' },
  { to: '/how-its-built', label: 'How it’s built' },
  { to: '/recaps', label: 'Recaps' },
] as const

export const DISCLAIMER =
  'Paper trading: the account holds simulated money and no order reaches a real market. Figures are published from the trading machine on a schedule, so they are delayed. This is not investment advice.'

/**
 * How fresh the figures are, as the pipeline really runs: the home box's
 * export (every 15 minutes, docs/public-portfolio.md) and the deploy schedule
 * in .github/workflows/portfolio.yml. Keep it in step with both. Equity stops
 * at the last completed session by design (web/public_export.py).
 */
export const CADENCE =
  'The trading machine exports its figures about every 15 minutes, and the site is rebuilt with the latest export about every 30 minutes during US market hours on weekdays and about every 6 hours at other times. Positions, trades and the research board can therefore trail the account by up to about 45 minutes during the session and about 6 hours outside it. Equity and returns are shown as of the end of the last completed session.'

export const LOADING = 'Loading the latest published figures…'
