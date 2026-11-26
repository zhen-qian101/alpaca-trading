import { useEffect, useRef, useState } from 'react'

import {
  isBuilderError,
  type ApprovalRequest,
  type EpochSeconds,
  type LookPresent,
  type PublisherApproval,
} from '../../contract.ts'
import { Age, BuilderErrorNote, ErrorNote, LiveStatus, Panel, Skeleton } from '../../components/ui.tsx'
import {
  canRequestApproval,
  homeBoxProblem,
  inApprovalWindow,
  intendedApproval,
  isApprovalPending,
  lookReflects,
  newestRequest,
  renderStartMs,
} from '../../lib/approval.ts'
import { useNow } from '../../lib/now.ts'
import { useApproveLook, useSnapshot } from '../../lib/queries.ts'
import { isStaleStatus, useSnapshotStatus } from '../../lib/status.ts'
import { formatClock, formatCountdown, formatIsoDate, isoToEpoch } from '../../lib/time.ts'
import { useDocumentTitle } from '../../lib/useDocumentTitle.ts'

function RenderCountdown({ date }: { date: string }) {
  const now = useNow()
  const at = renderStartMs(date)
  const melbourne = formatClock(at / 1000, 'melbourne')
  const left = at - now
  return (
    <p className="m-0 text-[0.8125rem]">
      {left > 0 ? (
        <>
          Render at <span className="mono">20:00</span> ET (<span className="mono">{melbourne}</span> Melbourne), in{' '}
          <span className="mono font-semibold" role="timer" aria-live="off">
            {formatCountdown(left)}
          </span>
        </>
      ) : (
        <>
          The <span className="mono">20:00</span> ET render for this date has started or finished.
        </>
      )}
    </p>
  )
}

interface VerdictInput {
  look: LookPresent
  /** The newest request, while the home box has not finished with it and the record does not reflect it. */
  pending: ApprovalRequest | null
  /** The home box's last failed apply of `pending`, which it will retry. */
  retrying: string | null
  /** The newest request, refused for good, when the record does not say what it asked for. */
  notApplied: { request: ApprovalRequest; outcome: string } | null
  /** The 20:00 ET render of look.date has started. */
  rendered: boolean
  renderAt: number
}

function verdictOf({ look, pending, retrying, notApplied, rendered, renderAt }: VerdictInput): {
  tone: 'ok' | 'warn' | 'bad' | 'neutral'
  badge: string
  text: string
} {
  const day = formatIsoDate(look.date)
  // What the video was made with, once the render has started: the look only
  // when it was signed off BEFORE 20:00 ET (the render reads it as it starts).
  const approvedAt = look.approved === true ? isoToEpoch(look.approved_at) : null
  const signedOffLate = approvedAt !== null && approvedAt * 1000 >= renderAt
  const renderedWith = look.approved === true && !signedOffLate ? 'this look' : 'the locked keyframe'

  if (notApplied) {
    const what = notApplied.request.approved ? 'sign-off' : 'withdrawal'
    const so = rendered
      ? `the render used ${renderedWith}`
      : look.approved === true
        ? 'the look stays signed off'
        : "tonight's render still uses the locked keyframe"
    return {
      tone: 'bad',
      badge: 'Not applied',
      text: `The home box did not apply this ${what} (${notApplied.outcome}), so ${so}.${rendered ? '' : ' Send it again, or use the local dashboard.'}`,
    }
  }
  if (pending) {
    const badge = pending.approved ? 'Sign-off requested' : 'Withdrawal requested'
    const what = pending.approved ? 'sign-off' : 'withdrawal'
    if (rendered) {
      return {
        tone: 'warn',
        badge,
        text: `The 20:00 ET render started before the home box applied this ${what}, so it will not be applied: the render used ${renderedWith}.`,
      }
    }
    if (retrying) {
      return {
        tone: 'warn',
        badge,
        text: `The home box could not apply this request yet (${retrying}) and will try again on its next report.`,
      }
    }
    return pending.approved
      ? { tone: 'warn', badge, text: `Sign-off requested — waiting for the home box to apply it. Until it does, tonight's render still uses the locked keyframe.` }
      : { tone: 'warn', badge, text: `Withdrawal requested — waiting for the home box to apply it. Until it does, the ${day} look stays signed off.` }
  }
  if (rendered) {
    if (look.approved === true) {
      return signedOffLate
        ? { tone: 'warn', badge: 'Signed off late', text: `Signed off after the 20:00 ET render had started, so the ${day} video used the locked keyframe.` }
        : { tone: 'ok', badge: 'Signed off', text: `Signed off: the ${day} render used this look.` }
    }
    if (look.approved === false) {
      return { tone: 'bad', badge: 'Refused', text: `Refused: the render used the locked keyframe instead of this look.` }
    }
    return { tone: 'neutral', badge: 'Not signed off', text: `Not signed off, so the ${day} render used the locked keyframe.` }
  }
  if (look.approved === true) {
    return { tone: 'ok', badge: 'Signed off', text: `Signed off: the ${day} look is what tonight's render will use.` }
  }
  if (look.approved === false) {
    return { tone: 'bad', badge: 'Refused', text: `Refused: tonight's render uses the locked keyframe instead of this look.` }
  }
  return {
    tone: 'warn',
    badge: 'Not signed off',
    text: `Not signed off yet, so tonight's render falls back to the locked keyframe. Sign off to use the ${day} look.`,
  }
}

function Verdict({
  look,
  request,
  report,
  asOf,
}: {
  look: LookPresent
  request: ApprovalRequest | null | undefined
  report: PublisherApproval | null | undefined
  /** The snapshot's as_of, for the "has not reported since" warning. */
  asOf: EpochSeconds | null
}) {
  const mutation = useApproveLook()
  const [confirming, setConfirming] = useState(false)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const headingRef = useRef<HTMLHeadingElement>(null)
  const confirmWasOpen = useRef(false)
  const homeBoxSilent = useSnapshotStatus().freshness === 'stale'
  // The request this page just recorded is kept as well as the snapshot's
  // copy, so the "waiting for the home box" state cannot flicker away if a
  // poll lands before the Worker's answer includes it. The newer one wins,
  // and the home box's report on it decides whether it is still pending.
  const mine = mutation.data?.request
  const latest = newestRequest(request, mine?.date === look.date ? mine : null)
  const pending = isApprovalPending(latest, look, report) ? latest : null
  const problem = homeBoxProblem(latest, report)
  const notApplied =
    latest && problem?.state === 'failed' && !lookReflects(latest, look) ? { request: latest, outcome: problem.outcome } : null
  const shown = pending ?? notApplied?.request ?? null
  const intended = intendedApproval(latest, look, report)
  const next = !intended
  const now = useNow()
  const renderAt = renderStartMs(look.date)
  const rendered = now >= renderAt
  const verdict = verdictOf({
    look,
    pending,
    retrying: pending && problem?.state === 'retrying' ? problem.outcome : null,
    notApplied,
    rendered,
    renderAt,
  })
  const allowed = canRequestApproval(look.date, now)
  const day = formatIsoDate(look.date)

  const showConfirm = confirming && allowed

  // Focus moves into the confirmation when it opens, and back to the button
  // that opened it when it closes (Cancel, Escape, or the answer arriving).
  // Closing unmounts the focused control, which would otherwise drop focus to
  // <body> and lose a keyboard or screen-reader user's place.
  useEffect(() => {
    if (showConfirm) {
      confirmWasOpen.current = true
      confirmRef.current?.focus()
      return
    }
    if (!confirmWasOpen.current) return
    confirmWasOpen.current = false
    const active = document.activeElement
    if (active !== null && active !== document.body) return // the user moved on; leave them there
    const trigger = triggerRef.current
    if (trigger && !trigger.disabled) trigger.focus()
    else headingRef.current?.focus()
  }, [showConfirm])

  const submit = () => {
    mutation.mutate(
      { date: look.date, approved: next },
      { onSettled: () => setConfirming(false) },
    )
  }

  const actionLabel = pending
    ? next
      ? 'Keep it signed off'
      : 'Cancel the request'
    : next
      ? 'Sign off this look'
      : 'Withdraw sign-off'

  return (
    <section
      aria-labelledby="verdict-title"
      className="panel"
      data-tone={verdict.tone}
      style={{ borderColor: `var(--${verdict.tone === 'ok' ? 'up' : verdict.tone === 'bad' ? 'down' : verdict.tone === 'warn' ? 'warn' : 'rule'})` }}
    >
      <div className="panel__body flex flex-wrap items-start justify-between gap-4">
        {/* Not a live region itself: it holds ages that tick every second. */}
        <div className="min-w-0 max-w-[68ch]">
          <LiveStatus message={`${verdict.badge}. ${verdict.text}`} />
          <h2
            id="verdict-title"
            ref={headingRef}
            tabIndex={-1}
            className="m-0 flex flex-wrap items-center gap-2 text-base outline-none"
          >
            <span className={`badge badge--${verdict.tone}`}>{verdict.badge}</span>
          </h2>
          <p className="m-0 mt-1.5 text-[0.9375rem]">{verdict.text}</p>
          {shown ? (
            <p className="m-0 mt-1 text-xs text-muted">
              Requested by {shown.requested_by}, <Age epoch={isoToEpoch(shown.requested_at)} />.
              {pending && !rendered ? ' The home box picks requests up on its next report, about once a minute.' : ''}
            </p>
          ) : look.approved_at ? (
            <p className="m-0 mt-1 text-xs text-muted">
              Signed off <Age epoch={isoToEpoch(look.approved_at)} />.
            </p>
          ) : null}
          <div className="mt-2">
            <RenderCountdown date={look.date} />
          </div>
        </div>

        <div className="flex flex-col items-start gap-2">
          {!showConfirm ? (
            <button
              ref={triggerRef}
              type="button"
              className={next && allowed ? 'btn btn--primary' : 'btn'}
              disabled={!allowed || mutation.isPending}
              onClick={() => {
                mutation.reset()
                setConfirming(true)
              }}
            >
              {actionLabel}
            </button>
          ) : (
            <div
              className="rounded-md border border-rule-strong bg-raised p-3"
              role="group"
              aria-labelledby="confirm-text"
              onKeyDown={(e) => {
                if (e.key === 'Escape') setConfirming(false)
              }}
            >
              <p id="confirm-text" className="m-0 max-w-[36ch] text-[0.8125rem]">
                {next
                  ? `Sign off the ${day} look? The 20:00 ET render uses it once the home box has applied it.`
                  : `Withdraw the sign-off? The 20:00 ET render will use the locked keyframe.`}
              </p>
              {homeBoxSilent ? (
                <p className="note note--warn m-0 mt-2 max-w-[36ch] text-[0.8125rem]">
                  The home box has not reported since{' '}
                  {asOf !== null ? <span className="mono">{formatClock(asOf, 'et')} ET</span> : 'its last snapshot'}. The
                  request is kept, but it is applied only when the home box reports again, and not at all if that is after
                  20:00 ET.
                </p>
              ) : null}
              <div className="mt-2 flex flex-wrap gap-2">
                <button ref={confirmRef} type="button" className="btn btn--primary" disabled={mutation.isPending} onClick={submit}>
                  {mutation.isPending ? 'Sending…' : next ? 'Confirm sign-off' : 'Confirm withdrawal'}
                </button>
                <button type="button" className="btn" disabled={mutation.isPending} onClick={() => setConfirming(false)}>
                  Cancel
                </button>
              </div>
            </div>
          )}
          {!allowed ? (
            <p className="m-0 max-w-[36ch] text-xs text-muted">
              {inApprovalWindow(look.date, now) && rendered
                ? 'Sign-off from here closed when the 20:00 ET render started. If the video will be rendered again, sign off on the home box’s local dashboard.'
                : 'Sign-off is accepted from the day before to three days after the session date, until its 20:00 ET render starts.'}
            </p>
          ) : null}
        </div>
      </div>
      {mutation.isError ? (
        <div className="px-3.5 pb-3">
          <ErrorNote error={mutation.error} title="The request was not saved." />
        </div>
      ) : null}
    </section>
  )
}

function Shot({
  src,
  alt,
  variant,
}: {
  src: string
  alt: string
  variant: 'keyframe' | 'face' | 'plate'
}) {
  // Keyed by src at the call site, so a new image starts with a clean slate.
  const [failed, setFailed] = useState(false)
  if (failed) {
    return <p className="note m-0">The image is not available yet: the home box uploads it with the look.</p>
  }
  return (
    <div className={`shot ${variant === 'face' ? 'shot--face' : variant === 'plate' ? 'shot--plate' : ''}`}>
      <img src={src} alt={alt} loading="lazy" decoding="async" onError={() => setFailed(true)} />
    </div>
  )
}

function LookDetail({ look }: { look: LookPresent }) {
  const w = look.weather
  const s = look.style
  const outfit = s?.outfit ?? 'the outfit'
  return (
    <div className="mt-4 grid gap-4 lg:grid-cols-12">
      <Panel id="keyframe" title="Stage keyframe" className="lg:col-span-8" meta={<span>what the render starts from</span>}>
        <Shot key={look.keyframe_url} src={look.keyframe_url} alt={`Stage keyframe for ${look.date}: ${outfit}`} variant="keyframe" />
      </Panel>
      <Panel id="face" title="Face at 3×" className="lg:col-span-4" meta={<span>mouth and eyes; no score catches these</span>}>
        <Shot key={`face-${look.keyframe_url}`} src={look.keyframe_url} alt="The keyframe's face, magnified three times" variant="face" />
      </Panel>
      <Panel id="plate" title="Wardrobe plate" className="lg:col-span-4" meta={s ? <span className="mono">{s.slug}</span> : null}>
        <Shot key={look.plate_url} src={look.plate_url} alt={`Wardrobe reference plate for ${look.date}: ${outfit}`} variant="plate" />
      </Panel>
      <Panel id="why" title="Why this look" className="lg:col-span-8" meta={w ? <span>forecast for {w.date}</span> : null}>
        <div className="panel__body">
          {s ? <p className="m-0 max-w-[68ch] text-[0.9375rem]">{s.rationale}</p> : <p className="m-0 text-muted">The stylist left no rationale.</p>}
          <dl className="m-0 mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-[0.8125rem] sm:grid-cols-4">
            <div>
              <dt className="text-xs text-muted">Weather</dt>
              <dd className="m-0">
                {w ? (
                  <>
                    <span className="mono">
                      {w.tmin_c}–{w.tmax_c} °C
                    </span>
                    , {w.sky}
                  </>
                ) : (
                  '—'
                )}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Rain</dt>
              <dd className="m-0 mono">{w ? `${w.precip_mm} mm` : '—'}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Plate identity</dt>
              <dd className="m-0 mono">{look.plate_identity?.toFixed(3) ?? '—'}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Keyframe identity</dt>
              <dd className="m-0 mono">{look.keyframe_identity?.toFixed(3) ?? '—'}</dd>
            </div>
          </dl>
          <p className="m-0 mt-2 text-xs text-muted">
            Identity is face similarity to the host, from 0 to 1. {w ? `Season: ${w.season}.` : ''}
          </p>
          {s ? (
            <details className="disclosure -mx-3.5 mt-3 border-t border-rule">
              <summary>The prompt the stylist wrote</summary>
              <p className="m-0 px-3.5 pb-2 mono text-[0.8125rem]">{s.outfit}</p>
              {s.hair ? <p className="m-0 px-3.5 pb-2 text-[0.8125rem]">Hair: {s.hair}</p> : null}
              {s.jewellery ? <p className="m-0 px-3.5 pb-2 text-[0.8125rem]">Jewellery: {s.jewellery}</p> : null}
            </details>
          ) : null}
        </div>
      </Panel>
    </div>
  )
}

/** Sign off (or withdraw) the presenter's outfit before tonight's render. */
export function LookPage() {
  useDocumentTitle('Look')
  const { data: snapshot } = useSnapshot()
  const stale = isStaleStatus(useSnapshotStatus())
  const look = snapshot?.look

  let body
  if (!snapshot) {
    body = <Skeleton className="h-40" />
  } else if (!look || isBuilderError(look)) {
    body = <BuilderErrorNote what="tonight's look" message={look?.error ?? 'no look section'} />
  } else if (!look.stylist_enabled) {
    body = (
      <div className="panel panel__body">
        <p className="m-0">The stylist is switched off, so the locked keyframe renders every night. Nothing to review.</p>
      </div>
    )
  } else if (!look.exists) {
    body = (
      <div className="panel panel__body">
        <p className="m-0">
          No look was built for {formatIsoDate(look.date)}. Tonight's render uses the locked keyframe.
        </p>
        <div className="mt-2">
          <RenderCountdown date={look.date} />
        </div>
      </div>
    )
  } else {
    body = (
      <>
        <Verdict
          look={look}
          request={snapshot.approval_request}
          report={snapshot.publisher.approval}
          asOf={isoToEpoch(snapshot.as_of)}
        />
        <LookDetail look={look} />
      </>
    )
  }

  return (
    <>
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h1 className="m-0 text-2xl">Tonight's look</h1>
          {look && !isBuilderError(look) ? (
            <p className="m-0 mt-0.5 text-[0.8125rem] text-muted">
              {formatIsoDate(look.date)}, presented by {look.host}
            </p>
          ) : null}
        </div>
        {stale ? <span className="tag tag--warn">Stale: the home box has not reported recently</span> : null}
      </div>
      {body}
    </>
  )
}
