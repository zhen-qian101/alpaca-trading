/**
 * The recap player's YouTube embed, ported from the public portfolio
 * (apps/portfolio/src/lib/youtube.ts).
 *
 * Nothing is requested from YouTube until the viewer presses play: the page
 * shows the R2 cover (or a placeholder) as a button, and only that click (or
 * a chapter click) creates the iframe. The player comes from the no-cookie
 * domain, the only frame source public/_headers allows.
 */
import { YOUTUBE_ID_RE } from '../contract.ts'

export const EMBED_ORIGIN = 'https://www.youtube-nocookie.com'

export function isYoutubeId(value: unknown): value is string {
  return typeof value === 'string' && YOUTUBE_ID_RE.test(value)
}

/**
 * The no-cookie embed URL, autoplaying (the click that creates it is the
 * viewer's play). `start` is whole seconds into the video; 0 omits it.
 */
export function embedUrl(id: string, start = 0): string {
  if (!isYoutubeId(id)) throw new Error('not a YouTube video id')
  const at = Number.isFinite(start) ? Math.max(0, Math.floor(start)) : 0
  return `${EMBED_ORIGIN}/embed/${id}?autoplay=1&rel=0&playsinline=1${at > 0 ? `&start=${at}` : ''}`
}

/** The video's page on youtube.com, for when the embed will not play. */
export function watchUrl(id: string): string {
  if (!isYoutubeId(id)) throw new Error('not a YouTube video id')
  return `https://www.youtube.com/watch?v=${id}`
}
