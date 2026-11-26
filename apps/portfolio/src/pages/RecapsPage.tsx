import { useEffect, useRef, useState } from 'react'

import { Unavailable } from '../components/Holdings.tsx'
import { Page } from '../components/Layout.tsx'
import { LOADING } from '../lib/copy.ts'
import { usePortfolio } from '../data/portfolioContext.ts'
import type { Recap } from '../data/portfolio.ts'
import { clock, longDate, shortDate, spokenDuration } from '../lib/format.ts'
import { embedUrl, watchUrl } from '../lib/youtube.ts'

/**
 * A click-to-play stand-in. Nothing is requested from YouTube, no thumbnail
 * and no script, until the visitor presses play; then the no-cookie player
 * replaces it, and keyboard focus moves into the player (the button that had
 * it is gone, and focus would otherwise fall back to the top of the page).
 */
function Player({ recap }: { recap: Recap }) {
  const [playing, setPlaying] = useState(false)
  const frameRef = useRef<HTMLIFrameElement>(null)
  useEffect(() => {
    if (playing) frameRef.current?.focus()
  }, [playing])
  // Labelled by its date alone: a language model writes the video's own title.
  const day = longDate(recap.date)
  const length = spokenDuration(recap.duration_s)
  return (
    <div className="player">
      {playing ? (
        <iframe
          ref={frameRef}
          src={embedUrl(recap.youtube_id)}
          title={`Recap for ${day}`}
          allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
          allowFullScreen
          referrerPolicy="strict-origin-when-cross-origin"
          // The player is the one third-party request on the site.
          loading="eager"
        />
      ) : (
        <button
          type="button"
          className="facade"
          onClick={() => setPlaying(true)}
          aria-label={`Play the recap for ${day}${length ? `, ${length}` : ''}. Loads the YouTube player.`}
        >
          <span className="facade__date" aria-hidden="true">
            {shortDate(recap.date)}
          </span>
          <span className="facade__foot" aria-hidden="true">
            <span className="num">{clock(recap.duration_s)}</span>
            <span className="facade__play">
              <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M7 4.5v15l12.5-7.5Z" fill="currentColor" />
              </svg>
            </span>
          </span>
        </button>
      )}
    </div>
  )
}

export function RecapsPage() {
  const state = usePortfolio()
  const recaps = state.status === 'ready' ? state.data.recaps : null
  return (
    <Page
      title="Recaps"
      heading="Recaps"
      intro={
        <>
          <p className="lede">
            After the US close each weekday, a computer at home makes a short AI-generated video about the market day and
            the account. Some Sundays get one about the week. These are the English editions published on YouTube,
            newest first.
          </p>
          <div className="notice">
            <p className="small">
              <strong>What the videos show.</strong> A language model writes each script and YouTube title from
              research’s summary of the market and the account’s own figures. On weekdays an AI-generated presenter
              reads the script; the Sunday editions are cartoons. Most of the videos also show the live trading
              dashboard, including market candles and current prices from the market-data feeds.
            </p>
            <p className="small">
              The rest of this site is different: its own figures, tables and charts contain no third-party market data
              and no model-written text. That is why each video is listed here by its date only.
            </p>
          </div>
          <p className="small muted prose">
            Nothing loads from YouTube until you press play; the player then comes from youtube-nocookie.com. The
            videos describe a paper account. They are not investment advice.
          </p>
        </>
      }
    >
      {state.status === 'loading' ? (
        <p className="muted">{LOADING}</p>
      ) : state.status === 'ready' && !recaps ? (
        <Unavailable what="recap list" />
      ) : recaps && recaps.length === 0 ? (
        <p>No recap has been published yet.</p>
      ) : recaps ? (
        <ul className="recap-grid">
          {recaps.map((r) => (
            <li key={r.youtube_id}>
              <Player recap={r} />
              <div className="recap-meta">
                <h2>
                  Recap for <time dateTime={r.date}>{longDate(r.date)}</time>
                </h2>
                <p className="small muted">
                  {r.duration_s !== null ? <>{clock(r.duration_s)}. </> : null}
                  <a href={watchUrl(r.youtube_id)} rel="noopener noreferrer">
                    Watch on YouTube
                  </a>
                </p>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">Recaps appear here once figures are published.</p>
      )}
    </Page>
  )
}
