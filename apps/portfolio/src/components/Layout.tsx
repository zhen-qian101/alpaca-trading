import { useEffect, useRef, useState, type ReactNode } from 'react'

import { usePortfolio } from '../data/portfolioContext.ts'
import { DISCLAIMER, NAV } from '../lib/copy.ts'
import { etDateTime } from '../lib/format.ts'
import { isStale, NORMAL_REFRESH_HOURS } from '../lib/freshness.ts'
import { LICENSES_PATH, TRADINGVIEW_NOTICE } from '../lib/licenses.ts'
import { Link } from '../lib/router.tsx'
import { useRouter } from '../lib/routerContext.ts'
import { useTheme } from '../lib/themeContext.ts'
import { site } from '../site.ts'
import { Mark } from './Mark.tsx'

function ThemeToggle() {
  const { theme, toggle } = useTheme()
  const next = theme === 'dark' ? 'light' : 'dark'
  return (
    <button type="button" className="icon-button" onClick={toggle} aria-label={`Use ${next} theme`} title={`Use ${next} theme`}>
      {theme === 'dark' ? (
        <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
        </svg>
      ) : (
        <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round">
          <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z" />
        </svg>
      )}
    </button>
  )
}

function Masthead() {
  const { path } = useRouter()
  return (
    <header className="masthead">
      <div className="wrap masthead__row">
        <Link to="/" className="wordmark">
          <Mark />
          <span>{site.title}</span>
        </Link>
        <nav className="nav" aria-label="Main">
          <ul>
            {NAV.map((item) => (
              <li key={item.to}>
                <Link to={item.to} aria-current={path === item.to ? 'page' : undefined}>
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <div className="masthead__tools">
          <ThemeToggle />
        </div>
      </div>
    </header>
  )
}

/** Shown on every page, whatever loaded, so a deep link to /trades sees it too. */
function DataNotice() {
  const state = usePortfolio()
  // Read the clock once per visit: the figures are a static file.
  const [now] = useState(() => Date.now())
  if (state.status === 'ready' && isStale(state.data.meta.as_of, now)) {
    return (
      <div className="wrap pt-6">
        <div className="notice notice--loss">
          <p>
            <strong>
              These figures were published on {etDateTime(state.data.meta.as_of)} and have not been updated since.
            </strong>{' '}
            The site normally refreshes at least every {NORMAL_REFRESH_HOURS} hours, weekends and holidays included, so
            they may be well out of date.
          </p>
        </div>
      </div>
    )
  }
  if (state.status === 'missing') {
    return (
      <div className="wrap pt-6">
        <div className="notice notice--loss">
          <p>
            <strong>Account figures are not published right now.</strong> The pages below still explain how the
            system works; the numbers appear with the next published export.
          </p>
        </div>
      </div>
    )
  }
  if (state.status === 'invalid') {
    return (
      <div className="wrap pt-6">
        <div className="notice notice--loss">
          <p>
            <strong>The published figures failed this page’s checks, so none of them are shown.</strong> The pages
            below still explain how the system works.
          </p>
        </div>
      </div>
    )
  }
  return null
}

function Footer() {
  const state = usePortfolio()
  const asOf = state.status === 'ready' ? state.data.meta.as_of : null
  const year = asOf ? asOf.slice(0, 4) : null
  return (
    <footer className="footer">
      <div className="wrap">
        <p className="prose">
          {DISCLAIMER}
          {asOf ? ` Figures as of ${etDateTime(asOf)}.` : ''}
        </p>
        <p className="prose">
          Not affiliated with Alpaca, the broker whose paper-trading API this system uses, or with any other company
          named on this site. Charts drawn with{' '}
          <a href="https://www.tradingview.com/lightweight-charts/" rel="noopener noreferrer">
            TradingView Lightweight Charts
          </a>
          .
        </p>
        {/* The attribution notice from the library's NOTICE file (src/lib/licenses.ts). */}
        <p className="prose">
          {TRADINGVIEW_NOTICE.product} {TRADINGVIEW_NOTICE.copyright}{' '}
          <a href={TRADINGVIEW_NOTICE.url} rel="noopener noreferrer">
            {TRADINGVIEW_NOTICE.url}
          </a>
        </p>
        <ul>
          <li>
            <a href={site.repoUrl} rel="noopener noreferrer">
              Source on GitHub
            </a>
          </li>
          <li>
            {/* A file, not a page: a plain link, so the browser loads it. */}
            <a href={LICENSES_PATH}>Open-source licences</a>
          </li>
          {site.author.links.map((link) => (
            <li key={link.url}>
              <a href={link.url} rel="noopener noreferrer me">
                {link.label}
              </a>
            </li>
          ))}
        </ul>
        <p>{site.author.name ? `© ${year ? `${year} ` : ''}${site.author.name}. All rights reserved.` : 'All rights reserved.'}</p>
      </div>
    </footer>
  )
}

export function Layout({ children }: { children: ReactNode }) {
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <Masthead />
      <DataNotice />
      <main id="main" tabIndex={-1}>
        {children}
      </main>
      <Footer />
    </>
  )
}

/**
 * One page: sets the document title and, after an in-site navigation, moves
 * focus to the page heading so screen readers announce the new page.
 */
export function Page({
  title,
  heading,
  intro,
  children,
  className = '',
}: {
  title: string
  heading: ReactNode
  intro?: ReactNode
  children: ReactNode
  className?: string
}) {
  const { navigated, path } = useRouter()
  const headingRef = useRef<HTMLHeadingElement>(null)

  useEffect(() => {
    document.title = title === site.title ? site.title : `${title} | ${site.title}`
  }, [title])

  useEffect(() => {
    if (navigated) headingRef.current?.focus()
  }, [navigated, path])

  return (
    <div className={`wrap page ${className}`}>
      <div className="page-head">
        <h1 ref={headingRef} tabIndex={-1}>
          {heading}
        </h1>
        {intro}
      </div>
      {children}
    </div>
  )
}
