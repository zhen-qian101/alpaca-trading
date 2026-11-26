import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'

import type { Edition } from '../../contract.ts'
import { Age, BiasTag, ErrorNote, Panel, Skeleton } from '../../components/ui.tsx'
import { isAuthError, isNotFound } from '../../lib/api.ts'
import { useHistory } from '../../lib/queries.ts'
import { readPref, writePref } from '../../lib/storage.ts'
import { formatIsoDate, isoToEpoch } from '../../lib/time.ts'
import { useDocumentTitle } from '../../lib/useDocumentTitle.ts'
import {
  chapterAt,
  chaptersFor,
  editionId,
  effectiveLang,
  groupByDate,
  hasZh,
  mmss,
  posterSrc,
  titleFor,
  videoSrc,
  type Lang,
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
                  <span className="text-xs text-muted">
                    {kindLabel(e)}
                    {hasZh(e) ? ', EN + 中文' : ''}
                  </span>
                </button>
              )
            })}
          </div>
        ))}
      </div>
    </Panel>
  )
}

function LangToggle({ lang, onChange }: { lang: Lang; onChange: (l: Lang) => void }) {
  return (
    <div className="seg" role="group" aria-label="Narration language">
      <button type="button" aria-pressed={lang === 'en'} onClick={() => onChange('en')} lang="en">
        EN
      </button>
      <button type="button" aria-pressed={lang === 'zh'} onClick={() => onChange('zh')} lang="zh">
        中文
      </button>
    </div>
  )
}

function Player({ edition, wantedLang, onLang }: { edition: Edition; wantedLang: Lang; onLang: (l: Lang) => void }) {
  const lang = effectiveLang(edition, wantedLang)
  const src = videoSrc(edition, lang)
  const videoRef = useRef<HTMLVideoElement>(null)
  const [duration, setDuration] = useState<number | null>(null)
  const [time, setTime] = useState(0)
  // The parent keys this component by edition + language, so a new source
  // always starts from a clean duration / position / error state.
  const [failed, setFailed] = useState(false)

  const chapters = useMemo(() => chaptersFor(edition, lang, duration), [edition, lang, duration])
  const current = chapterAt(chapters, time)
  const approximate = chapters.length > 0 && !chapters[0].exact
  const title = titleFor(edition, lang)

  const seek = (start: number) => {
    const v = videoRef.current
    if (!v) return
    v.currentTime = start
    void v.play().catch(() => {})
  }

  return (
    <div className="min-w-0">
      <div className="video-frame">
        <video
          key={src}
          ref={videoRef}
          src={src}
          poster={posterSrc(edition, lang)}
          controls
          preload="metadata"
          playsInline
          lang={lang === 'zh' ? 'zh' : 'en'}
          onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
          onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
          onError={() => setFailed(true)}
        >
          <p>Your browser cannot play this video.</p>
        </video>
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

      <div className="mt-3 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="m-0 text-xl leading-snug" lang={lang === 'zh' && edition.title_zh ? 'zh' : 'en'}>
            {title ?? edition.label ?? 'Recap'}
          </h2>
          <p className="m-0 mt-0.5 text-[0.8125rem] text-muted">
            {edition.label ?? kindLabel(edition)} for {formatIsoDate(edition.date)}
          </p>
        </div>
        {hasZh(edition) ? <LangToggle lang={lang} onChange={onLang} /> : null}
      </div>

      <dl className="m-0 mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-[0.8125rem] sm:grid-cols-4">
        <div>
          <dt className="text-xs text-muted">Runtime</dt>
          <dd className="m-0 mono">{mmss(lang === 'zh' && duration ? duration : edition.duration_s)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Format</dt>
          <dd className="m-0">{kindLabel(edition)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Voice</dt>
          <dd className="m-0 mono">{voiceName(lang === 'zh' ? edition.voice_zh : edition.voice)}</dd>
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
                  onClick={() => seek(c.start)}
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

/** The recap archive: pick an edition, watch it in English or Chinese, jump by chapter. */
export function BriefingPage() {
  useDocumentTitle('Briefing')
  const query = useHistory()
  const [params, setParams] = useSearchParams()
  const [wantedLang, setWantedLang] = useState<Lang>(() => (readPref('lang') === 'zh' ? 'zh' : 'en'))

  const editions = query.data?.briefings ?? []
  const requested = params.get('edition')
  const edition = editions.find((e) => editionId(e) === requested) ?? editions[0]
  const shownId = edition ? editionId(edition) : null

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
  if (known === null && query.data) setKnown(new Set(query.data.briefings.map(editionId)))
  const newest = editions[0]
  const announced =
    known !== null && newest && !known.has(editionId(newest)) && editionId(newest) !== shownId ? newest : null

  const pick = (id: string) => {
    setKnown((prev) => new Set([...(prev ?? []), id]))
    const next = new URLSearchParams(params)
    next.set('edition', id)
    setParams(next, { replace: true })
  }
  const setLang = (l: Lang) => {
    setWantedLang(l)
    writePref('lang', l)
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
  } else if (!edition) {
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
          <Player
            key={`${editionId(edition)}:${effectiveLang(edition, wantedLang)}`}
            edition={edition}
            wantedLang={wantedLang}
            onLang={setLang}
          />
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
