/** Embeds come from YouTube's no-cookie domain, and only after a click. */
export const EMBED_ORIGIN = 'https://www.youtube-nocookie.com'

const ID_RE = /^[A-Za-z0-9_-]{11}$/

export function embedUrl(id: string): string {
  if (!ID_RE.test(id)) throw new Error('not a YouTube video id')
  return `${EMBED_ORIGIN}/embed/${id}?autoplay=1&rel=0&playsinline=1`
}

export function watchUrl(id: string): string {
  if (!ID_RE.test(id)) throw new Error('not a YouTube video id')
  return `https://www.youtube.com/watch?v=${id}`
}
