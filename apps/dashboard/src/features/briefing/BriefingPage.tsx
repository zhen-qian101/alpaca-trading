import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'

import type { Edition } from '../../contract.ts'
import { Age, BiasTag, ErrorNote, Panel, Skeleton } from '../../components/ui.tsx'
import { isAuthError, isNotFound } from '../../lib/api.ts'
import { useHistory } from '../../lib/queries.ts'
import { formatIsoDate, isoToEpoch } from '../../lib/time.ts'
import { useDocumentTitle } from '../../lib/useDocumentTitle.ts'
import { embedUrl, watchUrl } from '../../lib/youtube.ts'
import {
  chapterAt,
  chaptersFor,
  editionId,
  groupByDate,
  mmss,
  playableEditions,
  playbackFor,
  type Chapter,
  type Playback,
} from './editions.ts'

function kindLabel(e: Edition): string {
  if (e.kind === 'slideshow') return 'Slideshow'
  return e.mode === 'story' ? 'Story video' : 'Video'
}

function voiceName(voice: string | null | undefined): string {
  if (!voice) return '—'
  return voice.replace(/^[a-z]{2}-[A-Z]{2}-/, '').replace(/Neural$/, '')
}

function EditionList({ editions, currentId, onPick }: { editions: Edition[]; currentId: string; onPick: (id: string) => void }) {
  return (
    <Panel id="editions" title="Editions" meta={<span>{editions.length} published</span>}>
      <div className="max-h-[34rem] overflow-y-auto">
        {groupByDate(editions).map((group) => (
          <div key={group.date} role="group" aria-labelledby={`ed-${group.date}`}>
            <h3 id={`ed-${group.date}`} className="m-0 px-3.5 pt-3 pb-1.5 text-xs text-muted">
              {formatIsoDate(group.date)}
            </h3>
            {group.editions.map((e) => {
              const id = editionId(e)
              return (
                <button
                  key={id}
                  type="button"
                  className="edition"
                  aria-current={id === currentId ? 'true' : undefined}
                  onClick={() => onPick(id)}
                >
                  <span className="min-w-0 font-semibold">{e.label ?? kindLabel(e)}</span>
                  <span className="mono text-xs text-muted">{mmss(e.duration_s)}</span>
                  <span className="min-w-0 truncate text-[0.8125rem] text-muted">{e.title ?? 'Untitled'}</span>
                  <span className="text-xs text-muted">{kindLabel(e)}</span>
                </button>
              )
            })}
          </div>
        ))}
      </div>
    </Panel>
  )
}

/** "Post-market recap for Wed, 4 Mar 2026". */
function editionName(e: Edition): string {
  return `${e.label ?? kindLabel(e)} for ${formatIsoDate(e.date)}`
}

/**
 * The YouTube player, started: a no-cookie embed, created fresh (keyed by
 * `n`) for each play or chapter jump. A new iframe rather than a new `src`, so
 * a jump adds no entry to the browser's back history.
 */
interface YoutubeRun {
  /** Whole seconds into the video. */
  start: number
  n: number
  /** Move keyboard focus into the player (the play button it replaces is gone). */
  focus: boolean
}

/**
 * The click-to-play stand-in for a YouTube edition: the R2 cover, or a plain
 * placeholder. Nothing is requested from YouTube, no thumbnail and no script,
 * until the viewer presses it (or picks a chapter).
 */
function Facade({ edition, onPlay }: { edition: Edition; onPlay: () => void }) {
  const [coverFailed, setCoverFailed] = useState(false)
  const runtime = edition.duration_s ? `, ${mmss(edition.duration_s)}` : ''
  return (
    <button
      type="button"
      className="facade"
      onClick={onPlay}
      aria-label={`Play ${editionName(edition)}${runtime}. Loads the YouTube player.`}
    >
      {edition.cover && !coverFailed ? (
        <img src={edition.cover} alt="" decoding="async" onError={() => setCoverFailed(true)} />
      ) : (
        <span className="facade__placeholder" aria-hidden="true">
          <span className="font-semibold">{edition.label ?? kindLabel(edition)}</span>
          <span className="mono text-xs">{formatIsoDate(edition.date)}</span>
        </span>
      )}
      <span className="facade__play" aria-hidden="true">
        <svg width="26" height="26" viewBox="0 0 24 24" focusable="false">
          <path d="M7 4.5v15l12.5-7.5Z" fill="currentColor" />
        </svg>
      </span>
      <span className="facade__note" aria-hidden="true">
        Plays from YouTube
      </span>
    </button>
  )
}

function Player({ edition, playback }: { edition: Edition; playback: Playback }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const frameRef = useRef<HTMLIFrameElement>(null)
  const [duration, setDuration] = useState<number | null>(null)
  const [time, setTime] = useState(0)
  // The parent keys this component by edition and player kind, so a new
  // edition (or one the publisher moved from its fallback mp4 to YouTube)
  // starts from a clean duration / position / error state, and a YouTube
  // edition from its play button.
  const [failed, setFailed] = useState(false)
  const [run, setRun] = useState<YoutubeRun | null>(null)

  useEffect(() => {
    if (run?.focus) frameRef.current?.focus()
  }, [run])

  const chapters = useMemo(() => chaptersFor(edition, duration), [edition, duration])
  // An embedded YouTube player does not report its position (no player API is
  // loaded), so only the <video> marks the chapter playing.
  const current = playback.kind === 'file' ? chapterAt(chapters, time) : -1
  const approximate = chapters.length > 0 && !chapters[0].exact
  const name = editionName(edition)

  const play = (start: number, focus: boolean) =>
    setRun((prev) => ({ start: Math.max(0, Math.floor(start)), n: (prev?.n ?? 0) + 1, focus }))

  const seek = (c: Chapter) => {
    if (playback.kind === 'youtube') {
      play(c.start, false)
      return
    }
    const v = videoRef.current
    if (!v) return
    v.currentTime = c.start
    void v.play().catch(() => {})
  }

  return (
    <div className="min-w-0">
      <div className="video-frame">
        {playback.kind === 'file' ? (
          <video
            key={playback.src}
            ref={videoRef}
            src={playback.src}
            poster={edition.cover}
            controls
            preload="metadata"
            playsInline
            lang="en"
            onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
            onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
            onError={() => setFailed(true)}
          >
            <p>Your browser cannot play this video.</p>
          </video>
        ) : run ? (
          <iframe
            key={run.n}
            ref={frameRef}
            src={embedUrl(playback.id, run.start)}
            title={`${name} (YouTube player)`}
            allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
            allowFullScreen
            // The page sends no referrer anywhere (public/_headers), but
            // YouTube's player refuses to play without one: this request alone
            // sends the dashboard's origin, never a path.
            referrerPolicy="strict-origin-when-cross-origin"
          />
        ) : (
          <Facade edition={edition} onPlay={() => play(0, true)} />
        )}
      </div>
      {failed ? (
        <div className="note note--error mt-2" role="alert">
          <p className="m-0">
            <strong>This video did not load.</strong> The file may not be uploaded yet, or your sign-in has expired.
          </p>
          <button type="button" className="btn mt-2" onClick={() => window.location.reload()}>
            Reload the page
          </button>
        </div>
      ) : null}

      <div className="mt-3 min-w-0">
        <h2 className="m-0 text-xl leading-snug">{edition.title ?? edition.label ?? 'Recap'}</h2>
        <p className="m-0 mt-0.5 text-[0.8125rem] text-muted">
          {name}
          {playback.kind === 'youtube' ? (
            <>
              {' · '}
              <a
                className="underline underline-offset-2 hover:text-ink"
                href={watchUrl(playback.id)}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open on YouTube
              </a>
            </>
          ) : (
            ' · Not on YouTube: playing the copy uploaded from home'
          )}
        </p>
      </div>

      <dl className="m-0 mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-[0.8125rem] sm:grid-cols-4">
        <div>
          <dt className="text-xs text-muted">Runtime</dt>
          <dd className="m-0 mono">{mmss(edition.duration_s)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Format</dt>
          <dd className="m-0">{kindLabel(edition)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Voice</dt>
          <dd className="m-0 mono">{voiceName(edition.voice)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Rendered</dt>
          <dd className="m-0">
            <Age epoch={isoToEpoch(edition.generated_at)} />
          </dd>
        </div>
      </dl>

      {chapters.length > 0 ? (
        <section aria-labelledby="chapters-title" className="panel mt-4">
          <header className="panel__head">
            <h3 id="chapters-title" className="panel__title">
              Running order
            </h3>
            <span className="panel__meta">
              {approximate ? 'Start times estimated from scene lengths' : 'Jump to a chapter'}
            </span>
          </header>
          <ol className="m-0 list-none p-0">
            {chapters.map((c, i) => (
              <li key={c.key}>
                <button
                  type="button"
                  className="chapter"
                  aria-current={i === current ? 'true' : undefined}
                  onClick={() => seek(c)}
                >
                  <span className="mono text-[0.8125rem] text-muted">
                    {c.exact ? '' : '≈'}
                    {mmss(c.start)}
                  </span>
                  <span className="min-w-0">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="mono font-bold">{c.label}</span>
                      <BiasTag bias={c.bias} />
                    </span>
                    <span className="block text-[0.8125rem] text-muted">{c.headline}</span>
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </section>
      ) : null}
    </div>
  )
}

/**
 * The recap archive: pick an edition, watch it (from YouTube after a click,
 * or the uploaded fallback mp4), jump by chapter. English only: the Chinese
 * cut stays on the home box.
 */
export function BriefingPage() {
  useDocumentTitle('Briefing')
  const query = useHistory()
  const [params, setParams] = useSearchParams()

  const editions = useMemo(() => playableEditions(query.data?.briefings ?? []), [query.data])
  const requested = params.get('edition')
  const edition = editions.find((e) => editionId(e) === requested) ?? editions[0]
  const shownId = edition ? editionId(edition) : null
  // Never null for a listed edition: playableEditions() kept only those.
  const playback = edition ? playbackFor(edition) : null

  // Pin the edition on screen into the URL. history.json is refetched every
  // few minutes and several editions are published a day: without the pin,
  // the default ("the newest") would change under the viewer and swap the
  // video they are watching for the new one, mid-playback.
  useEffect(() => {
    if (shownId === null || shownId === requested) return
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.set('edition', shownId)
        return next
      },
      { replace: true },
    )
  }, [shownId, requested, setParams])

  // Editions the viewer already knows about: those listed when the page first
  // loaded, plus any they have opened. A newer one is announced, not played.
  // Set once, during the render that first has the list (React's pattern for
  // state that follows earlier renders).
  const [known, setKnown] = useState<ReadonlySet<string> | null>(null)
  if (known === null && query.data) setKnown(new Set(editions.map(editionId)))
  const newest = editions[0]
  const announced =
    known !== null && newest && !known.has(editionId(newest)) && editionId(newest) !== shownId ? newest : null

  const pick = (id: string) => {
    setKnown((prev) => new Set([...(prev ?? []), id]))
    const next = new URLSearchParams(params)
    next.set('edition', id)
    setParams(next, { replace: true })
  }

  let body
  if (query.isPending) {
    body = (
      <div className="grid gap-4 lg:grid-cols-12">
        <Skeleton className="aspect-video lg:col-span-8" />
        <Skeleton className="h-64 lg:col-span-4" />
      </div>
    )
  } else if (query.isError && !query.data && !isNotFound(query.error)) {
    body = (
      <div className="max-w-2xl">
        <ErrorNote error={query.error} title="The list of recaps did not load." />
        {!isAuthError(query.error) ? (
          <button type="button" className="btn mt-3" onClick={() => void query.refetch()}>
            Try again
          </button>
        ) : null}
      </div>
    )
  } else if (!edition || !playback) {
    body = (
      <div className="max-w-2xl py-8">
        <p className="m-0 text-base">No recap has been published yet.</p>
        <p className="m-0 mt-1 text-muted">
          Editions appear here once the home box has rendered a recap and uploaded it. Check back after the evening
          render.
        </p>
      </div>
    )
  } else {
    body = (
      <div className="grid gap-4 lg:grid-cols-12">
        <div className="min-w-0 lg:col-span-8">
          {announced ? (
            <div className="note mb-3 flex flex-wrap items-center justify-between gap-2" role="status">
              <p className="m-0">
                New edition: <strong>{announced.label ?? kindLabel(announced)}</strong> for {formatIsoDate(announced.date)}.
              </p>
              <button type="button" className="btn" onClick={() => pick(editionId(announced))}>
                Watch it
              </button>
            </div>
          ) : null}
          <Player key={`${editionId(edition)}:${playback.kind}`} edition={edition} playback={playback} />
        </div>
        <div className="min-w-0 lg:col-span-4">
          <EditionList editions={editions} currentId={editionId(edition)} onPick={pick} />
        </div>
      </div>
    )
  }

  return (
    <>
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="m-0 text-2xl">Briefing</h1>
        {query.data ? (
          <p className="m-0 text-xs text-muted">
            Archive updated <Age epoch={isoToEpoch(query.data.generated_at)} />
          </p>
        ) : null}
      </div>
      {query.isError && query.data ? (
        <div className="mb-3 max-w-2xl">
          <ErrorNote error={query.error} title="The list of recaps did not refresh; showing the last one loaded." />
        </div>
      ) : null}
      {body}
    </>
  )
}
