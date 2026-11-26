import { isBuilderError, type Portfolio, type SnapshotResponse } from '../../contract.ts'
import { Age, BuilderErrorNote, LiveStatus, Skeleton } from '../../components/ui.tsx'
import { direction, money, pct, signedMoney, signedPct } from '../../lib/format.ts'
import { isoToEpoch } from '../../lib/time.ts'

function HaltMeter({ portfolio }: { portfolio: Portfolio }) {
  const h = portfolio.halt
  const lost = Math.max(0, -portfolio.day_pl_pct)
  const used = Math.min(1, Math.max(0, h.used_frac))
  const level = h.tripped || used >= 1 ? 'bad' : used >= 0.6 ? 'warn' : 'ok'
  return (
    <div className="min-w-0 flex-1 basis-72">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <h3 className="m-0 text-[0.8125rem]">Daily loss halt</h3>
        <p className="m-0 text-xs text-muted">
          {h.tripped ? (
            <span className="badge badge--bad">Halted: new buys blocked</span>
          ) : (
            <>
              <span className="mono text-ink">{pct(lost)}</span> lost of the{' '}
              <span className="mono text-ink">{pct(h.limit, 0)}</span> limit
            </>
          )}
        </p>
      </div>
      <div
        className="meter mt-1.5"
        role="meter"
        aria-label="Share of today's loss limit used"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(used * 100)}
        aria-valuetext={`${Math.round(used * 100)}% of the daily loss limit used`}
      >
        <div className="meter__fill" data-level={level} style={{ width: `${Math.max(used * 100, used > 0 ? 2 : 0)}%` }} />
      </div>
      <p className="m-0 mt-1 text-xs text-muted">Halts {h.applies_to}.</p>
      <LiveStatus
        message={h.tripped ? 'Daily loss halt tripped: new buys are blocked.' : 'Daily loss halt clear: new buys are allowed.'}
      />
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="m-0 mono text-[0.9375rem]">{value}</dd>
    </div>
  )
}

/**
 * The account readout across the top of the terminal: equity and today's
 * change as one tape line, the balances beside it, the loss halt beneath.
 */
export function AccountBand({
  snapshot,
  stale,
}: {
  snapshot: SnapshotResponse | undefined
  stale: boolean
}) {
  const asOf = snapshot ? isoToEpoch(snapshot.as_of) : null
  const portfolio = snapshot?.portfolio

  return (
    <section
      aria-labelledby="account-title"
      className="account border-b border-rule pb-4"
      data-stale={stale ? 'true' : undefined}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
        <h2 id="account-title" className="m-0 text-[0.8125rem] text-ink">
          Account
        </h2>
        {portfolio && !isBuilderError(portfolio) ? (
          <>
            <span className={`tag ${portfolio.paper ? 'tag--neutral' : 'tag--bad'}`}>
              {portfolio.paper ? 'Paper' : 'Live money'}
            </span>
            <span className="mono">{portfolio.status || 'status unknown'}</span>
          </>
        ) : null}
        {stale ? <span className="tag tag--warn">Stale</span> : null}
        {asOf !== null ? <Age epoch={asOf} prefix="reported " /> : null}
      </div>

      {!snapshot ? (
        <div className="mt-3 flex flex-wrap gap-6">
          <Skeleton className="h-10 w-64" />
          <Skeleton className="h-10 w-80" />
        </div>
      ) : isBuilderError(portfolio) || !portfolio ? (
        <div className="mt-3">
          <BuilderErrorNote what="the account" message={portfolio?.error ?? 'no account section'} />
        </div>
      ) : (
        <div className="mt-2 flex flex-wrap items-end gap-x-10 gap-y-4">
          {/* Not a live region: equity moves with every snapshot, and a screen
              reader would read it out each time. The halt has its own. */}
          <div className="min-w-0">
            <p className="m-0 text-xs text-muted">Equity</p>
            <p className="m-0 mono text-[2rem] leading-tight font-semibold tracking-tight sm:text-[2.5rem]">
              {money(portfolio.equity)}
            </p>
            <p className={`m-0 mono text-[0.9375rem] ${direction(portfolio.day_pl)}`}>
              {signedMoney(portfolio.day_pl)} ({signedPct(portfolio.day_pl_pct)}) today
            </p>
          </div>
          <dl className="m-0 grid grid-cols-2 gap-x-8 gap-y-2 sm:grid-cols-3">
            <Stat label="Cash" value={money(portfolio.cash)} />
            <Stat label="Buying power" value={money(portfolio.buying_power)} />
            <Stat label="Previous close" value={money(portfolio.last_equity)} />
          </dl>
          <HaltMeter portfolio={portfolio} />
        </div>
      )}
    </section>
  )
}
