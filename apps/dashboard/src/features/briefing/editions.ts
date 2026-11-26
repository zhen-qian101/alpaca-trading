import type { Edition, EditionClip, MediaUrl } from '../../contract.ts'
import { isYoutubeId } from '../../lib/youtube.ts'

/** Stable id of an edition: its archive directory, "<date>/<slot>". */
export function editionId(e: Edition): string {
  return `${e.date}/${e.slot}`
}

/** How an edition plays: the YouTube embed, or the fallback mp4 from R2. */
export type Playback = { kind: 'youtube'; id: string } | { kind: 'file'; src: MediaUrl }

function isMediaUrl(value: unknown): value is MediaUrl {
  return typeof value === 'string' && value.startsWith('/media/')
}

/**
 * The edition's player, or null when it has nothing playable. YouTube wins
 * when both are present: the publisher replaces the fallback mp4 with the id
 * once one appears, and the mp4 is on its way out of R2.
 */
export function playbackFor(e: Edition): Playback | null {
  if (isYoutubeId(e.youtube_id)) return { kind: 'youtube', id: e.youtube_id }
  if (isMediaUrl(e.briefing)) return { kind: 'file', src: e.briefing }
  return null
}

/**
 * The editions the page can offer. history.json should never list one with
 * neither a YouTube id nor a fallback mp4, nor a malformed id (which is never
 * put into an embed URL); if it does, the entry is left out rather than shown
 * as a player that cannot play.
 */
export function playableEditions(editions: readonly Edition[]): Edition[] {
  return editions.filter((e) => playbackFor(e) !== null)
}

/** Editions grouped by session date, newest first, keeping history.json's order. */
export function groupByDate(editions: readonly Edition[]): { date: string; editions: Edition[] }[] {
  const groups: { date: string; editions: Edition[] }[] = []
  for (const e of editions) {
    const last = groups.at(-1)
    if (last && last.date === e.date) last.editions.push(e)
    else groups.push({ date: e.date, editions: [e] })
  }
  return groups
}

export interface Chapter {
  key: string
  /** "OVERVIEW", "DESK", a ticker, or a story character. */
  label: string
  headline: string
  bias: EditionClip['bias']
  /** Seconds into the full EN video. */
  start: number
  /**
   * True when the archive recorded this offset (legacy story renders). False
   * when it is estimated from the scene running times, which can drift by a
   * second or two against the final cut.
   */
  exact: boolean
}

/**
 * The running order as seek points into the full EN video, whichever player
 * shows it (YouTube serves the same EN cut as the fallback mp4).
 *
 * Legacy story renders record each chapter's offset (`start_s`). Everything
 * else is a plain concat of its scenes, so a scene starts where the previous
 * ones' running times add up to; that sum is scaled to the real length of the
 * video once a <video> knows it (`videoDuration`), else to the archived
 * `duration_s` (the final encode can pad a scene by a frame or two).
 */
export function chaptersFor(e: Edition, videoDuration: number | null): Chapter[] {
  const clips = e.clips ?? []
  if (clips.length === 0) return []
  const recorded = clips.every((c) => typeof c.start_s === 'number')

  if (recorded && clips.every((c) => c.file === undefined || c.file === e.briefing)) {
    return clips.map((c, i) => ({
      key: `${i}-${c.symbol}`,
      label: c.symbol,
      headline: c.headline,
      bias: c.bias,
      start: c.start_s ?? 0,
      exact: true,
    }))
  }

  const total = clips.reduce((sum, c) => sum + (c.duration_s || 0), 0)
  const target = videoDuration && Number.isFinite(videoDuration) && videoDuration > 0 ? videoDuration : e.duration_s
  const scale = total > 0 && target > 0 ? target / total : 1
  let at = 0
  return clips.map((c, i) => {
    const start = recorded ? (c.start_s ?? 0) * scale : at * scale
    at += c.duration_s || 0
    return { key: `${i}-${c.symbol}`, label: c.symbol, headline: c.headline, bias: c.bias, start, exact: false }
  })
}

/** Index of the chapter playing at `t` seconds; -1 before the first. */
export function chapterAt(chapters: readonly Chapter[], t: number): number {
  let idx = -1
  for (let i = 0; i < chapters.length; i++) {
    if (chapters[i].start <= t + 0.05) idx = i
    else break
  }
  return idx
}

/** "2:21" from seconds. */
export function mmss(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—'
  const s = Math.max(0, Math.round(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}
