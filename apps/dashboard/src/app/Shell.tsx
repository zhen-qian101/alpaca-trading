import { useMemo, type ReactNode } from 'react'
import { Link, NavLink, Outlet } from 'react-router'

import { SNAPSHOT_SCHEMA, type SnapshotResponse } from '../contract.ts'
import { describeError, isAuthError, SIGNED_OUT_MESSAGE } from '../lib/api.ts'
import { ageMs, fuseRemaining, pageFreshness } from '../lib/freshness.ts'
import { useNow } from '../lib/now.ts'
import { usePrefs } from '../lib/prefs.ts'
import { useSnapshot } from '../lib/queries.ts'
import { SnapshotStatusContext, useSnapshotStatus, type SnapshotStatus } from '../lib/status.ts'
import { usePageVisibility } from '../lib/visibility.ts'
import { formatAge, formatDateTime, isoToEpoch, ZONES } from '../lib/time.ts'

const NAV = [
  { to: '/', label: 'Terminal', end: true },
  { to: '/briefing', label: 'Briefing', end: false },
  { to: '/look', label: 'Look', end: false },
] as const

/**
 * The fuse: how much of the 3-minute freshness window the last report has
 * left. It burns down between polls; past 3 minutes it is spent and the page
 * says the home box has gone quiet.
 */
function Fuse({ snapshot, fetching }: { snapshot: SnapshotResponse | undefined; fetching: boolean }) {
  const now = useNow()
  const { freshness } = useSnapshotStatus()
  if (!snapshot) {
    return (
      <p className="fuse m-0 text-xs text-muted">
        <span className="fuse__label">{fetching ? 'Loading first report…' : 'No report yet'}</span>
      </p>
    )
  }
  const age = ageMs(snapshot.as_of, now)
  const remaining = fuseRemaining(snapshot.as_of, now)
  return (
    <p className="fuse m-0 text-xs" data-state={freshness}>
      <span className="fuse__track" aria-hidden="true">
        <span className="fuse__burn" style={{ width: `${Math.round(remaining * 100)}%` }} />
      </span>
      <span className="fuse__label tabular-nums">
        {age === null ? 'Report time unknown' : `as of ${formatAge(age)} ago`}
        {freshness === 'checking' ? ' · checking…' : ''}
      </span>
    </p>
  )
}

function ThemeToggle() {
  const { theme, setTheme } = usePrefs()
  const next = theme === 'dark' ? 'light' : 'dark'
  return (
    <button
      type="button"
      className="btn btn--quiet btn--icon"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
    >
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

/** Page-wide notices: signed out, unreachable, or a home box that stopped reporting. */
function StatusBanner({ snapshot, error }: { snapshot: SnapshotResponse | undefined; error: unknown }) {
  const status = useSnapshotStatus()
  const now = useNow()
  const { zone } = usePrefs()

  if (status.signedOut) {
    return (
      <div className="banner banner--bad mt-3">
        <p role="alert" className="m-0">
          <strong>{SIGNED_OUT_MESSAGE}</strong>
          {snapshot ? ' Numbers below are from before the session ended.' : ''}
        </p>
        <button type="button" className="btn" onClick={() => window.location.reload()}>
          Reload
        </button>
      </div>
    )
  }

  if (!snapshot && error) {
    return (
      <div className="banner banner--bad mt-3">
        <p role="alert" className="m-0">
          <strong>The dashboard could not load.</strong> {describeError(error)}
        </p>
      </div>
    )
  }

  if (!snapshot) return null
  if (snapshot.schema !== SNAPSHOT_SCHEMA) {
    return (
      <div className="banner banner--bad mt-3">
        <p role="alert" className="m-0">
          <strong>The home box sent snapshot format {String(snapshot.schema)}; this page reads format {SNAPSHOT_SCHEMA}.</strong>{' '}
          Numbers may be missing or misplaced until the dashboard is updated.
        </p>
      </div>
    )
  }
  const asOf = isoToEpoch(snapshot.as_of)
  const since = `${formatDateTime(asOf, zone)} ${ZONES[zone].label}`

  if (status.freshness === 'stale') {
    const age = ageMs(snapshot.as_of, now)
    return (
      <div className="banner mt-3">
        <p role="alert" className="m-0">
          <strong>Home box not reporting since {since}.</strong> Everything below is from that report.
          {status.failing ? ` The last poll also failed: ${describeError(error)}` : ''}
        </p>
        {age !== null ? <span className="text-xs tabular-nums">{formatAge(age)} without a report</span> : null}
      </div>
    )
  }

  if (status.failing) {
    return (
      <div className="banner mt-3">
        <p role="alert" className="m-0">
          <strong>The last refresh failed.</strong> {describeError(error)} Showing the report from {since}; retrying.
        </p>
      </div>
    )
  }
  return null
}

/**
 * Publishes the page-wide snapshot verdict. It ticks with the clock, but its
 * children are created by the Shell (which does not tick), so React skips
 * re-rendering them; only consumers of the memoised verdict update, and only
 * when it flips.
 */
function SnapshotStatusProvider({
  asOf,
  answeredAt,
  failing,
  signedOut,
  children,
}: {
  asOf: string | undefined
  /** When the Worker last answered the snapshot poll (data or final error), wall-clock ms. */
  answeredAt: number
  failing: boolean
  signedOut: boolean
  children: ReactNode
}) {
  const now = useNow()
  const { hidden, resumedAt } = usePageVisibility()
  const freshness = asOf ? pageFreshness(asOf, now, { answeredAt, hidden, resumedAt }) : 'unknown'
  const status = useMemo<SnapshotStatus>(
    () => ({ freshness, failing, signedOut }),
    [freshness, failing, signedOut],
  )
  return <SnapshotStatusContext.Provider value={status}>{children}</SnapshotStatusContext.Provider>
}

export function Shell() {
  const snapshotQuery = useSnapshot()
  const snapshot = snapshotQuery.data
  const error = snapshotQuery.error

  return (
    <SnapshotStatusProvider
      asOf={snapshot?.as_of}
      answeredAt={Math.max(snapshotQuery.dataUpdatedAt, snapshotQuery.errorUpdatedAt)}
      failing={snapshotQuery.isError}
      signedOut={isAuthError(error)}
    >
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <div className="mx-auto flex max-w-[96rem] flex-wrap items-center gap-x-3 px-4 sm:gap-x-5">
          <Link to="/" className="wordmark flex min-h-12 items-center gap-2" aria-label="Alpaca Terminal home">
            <span aria-hidden="true" className="inline-block size-2.5 rounded-[2px] bg-signal" />
            <span>
              alpaca <span className="hidden text-muted sm:inline">terminal</span>
            </span>
          </Link>
          <nav aria-label="Main" className="order-last -mx-3 flex w-full sm:order-none sm:mx-0 sm:w-auto">
            {NAV.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.end} className="navlink">
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex min-h-12 items-center gap-1 sm:gap-2">
            <Fuse snapshot={snapshot} fetching={snapshotQuery.isFetching} />
            <ThemeToggle />
          </div>
        </div>
      </header>
      <div className="mx-auto max-w-[96rem] px-4">
        <StatusBanner snapshot={snapshot} error={error} />
      </div>
      <main id="main" tabIndex={-1} className="mx-auto max-w-[96rem] px-4 pt-4 pb-12 outline-none">
        <Outlet />
      </main>
      <footer className="mx-auto flex max-w-[96rem] flex-wrap justify-between gap-2 border-t border-rule px-4 py-4 text-xs text-muted">
        <span>Read-only mirror of the home box. The only thing you can change here is the look sign-off.</span>
        {snapshot ? (
          <span className="mono">
            publisher {snapshot.publisher.version} on {snapshot.publisher.host}
          </span>
        ) : null}
      </footer>
    </SnapshotStatusProvider>
  )
}
