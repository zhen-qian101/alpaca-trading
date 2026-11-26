import { useState, type ReactNode } from 'react'

import type { Bias, EpochSeconds } from '../contract.ts'
import { describeError, isAuthError } from '../lib/api.ts'
import { useNow } from '../lib/now.ts'
import { usePrefs } from '../lib/prefs.ts'
import { formatAgo, formatLongDateTime, ZONES } from '../lib/time.ts'

interface PanelProps {
  id: string
  title: ReactNode
  /** Right side of the header: source and age. */
  meta?: ReactNode
  /** Marks the panel as showing old data (hatched header, warn border, "stale" tag). */
  stale?: boolean
  className?: string
  children: ReactNode
}

/** A titled region. Its heading names it for assistive tech. */
export function Panel({ id, title, meta, stale = false, className = '', children }: PanelProps) {
  const headingId = `${id}-title`
  return (
    <section
      id={id}
      aria-labelledby={headingId}
      className={`panel ${className}`}
      data-stale={stale ? 'true' : undefined}
    >
      <header className="panel__head">
        <h2 id={headingId} className="panel__title">
          {title}
        </h2>
        <div className="panel__meta flex flex-wrap items-center gap-x-2 gap-y-1">
          {stale ? <span className="tag tag--warn">Stale</span> : null}
          {meta}
        </div>
      </header>
      {children}
    </section>
  )
}

/** A horizontally scrollable table region that keyboard users can scroll too. */
export function TableScroll({
  label,
  tall = false,
  children,
}: {
  label: string
  tall?: boolean
  children: ReactNode
}) {
  return (
    // tabIndex makes the scroll container reachable by keyboard (WCAG 2.1.1).
    <div className={`tbl-wrap ${tall ? 'tbl-wrap--tall' : ''}`} role="region" aria-label={label} tabIndex={0}>
      {children}
    </div>
  )
}

export function BiasTag({ bias }: { bias: Bias | null | undefined }) {
  if (!bias) return null
  return <span className={`tag tag--${bias}`}>{bias}</span>
}

/** "42 s ago", ticking; the absolute time in the zone is in the datetime + title. */
export function Age({ epoch, prefix = '' }: { epoch: EpochSeconds | null | undefined; prefix?: string }) {
  const now = useNow()
  const { zone } = usePrefs()
  if (epoch == null) return <span className="mono">—</span>
  const iso = new Date(epoch * 1000).toISOString()
  return (
    <time className="tabular-nums" dateTime={iso} title={`${formatLongDateTime(epoch, zone)} ${ZONES[zone].label}`}>
      {prefix}
      {formatAgo(epoch, now)}
    </time>
  )
}

/** A panel-level failure, specific about what failed. */
export function ErrorNote({ error, title }: { error: unknown; title: string }) {
  const signedOut = isAuthError(error)
  return (
    <div className="note note--error" role="alert">
      <p className="m-0">
        <strong>{title}</strong> {describeError(error)}
      </p>
      {signedOut ? (
        <button type="button" className="btn mt-2" onClick={() => window.location.reload()}>
          Reload
        </button>
      ) : null}
    </div>
  )
}

/** A builder on the home box reported an error for this section. */
export function BuilderErrorNote({ what, message }: { what: string; message: string }) {
  return (
    <div className="note note--error" role="alert">
      <p className="m-0">
        <strong>The home box could not build {what}.</strong> It reported: <span className="mono">{message}</span>
      </p>
    </div>
  )
}

export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden="true" />
}

/**
 * A visually hidden live region for STATE CHANGES only: a verdict flipping,
 * the loss halt tripping, the engine stopping. `message` must be a sentence
 * derived from that state, never a figure or an age that moves by itself,
 * because a screen reader speaks every change. null means the state is not
 * known yet.
 *
 * The first known message is the page as it loaded, which is on screen
 * anyway, so it is not spoken; every change after it is.
 */
export function LiveStatus({ message }: { message: string | null }) {
  const [baseline, setBaseline] = useState<string | null>(message)
  const [changed, setChanged] = useState(false)
  // State derived from the previous render (react.dev "storing information
  // from previous renders"); each setter runs at most once.
  if (baseline === null && message !== null) setBaseline(message)
  else if (!changed && baseline !== null && message !== null && message !== baseline) setChanged(true)
  return (
    <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {changed && message !== null ? message : ''}
    </p>
  )
}
