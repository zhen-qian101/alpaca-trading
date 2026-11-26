import type { Edition, EditionClip, MediaUrl } from '../../contract.ts'

export type Lang = 'en' | 'zh'

/** Stable id of an edition: its archive directory, "<date>/<slot>". */
export function editionId(e: Edition): string {
  return `${e.date}/${e.slot}`
}

export function hasZh(e: Edition): boolean {
  return typeof e.briefing_zh === 'string' && e.briefing_zh.length > 0
}

/** The language actually playable: zh only when the edition has a zh cut. */
export function effectiveLang(e: Edition, wanted: Lang): Lang {
  return wanted === 'zh' && hasZh(e) ? 'zh' : 'en'
}

export function videoSrc(e: Edition, lang: Lang): MediaUrl {
  return lang === 'zh' && e.briefing_zh ? e.briefing_zh : e.briefing
}

export function posterSrc(e: Edition, lang: Lang): MediaUrl | undefined {
  return lang === 'zh' ? (e.cover_zh ?? e.cover) : e.cover
}

export function titleFor(e: Edition, lang: Lang): string | null {
  return lang === 'zh' && e.title_zh ? e.title_zh : e.title
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
  /** Seconds into the playing video. */
  start: number
  /**
   * True when the archive recorded this offset (legacy story renders). False
   * when it is estimated from the scene running times, which can drift by a
   * second or two against the final cut.
   */
  exact: boolean
}

/**
 * The running order as seek points into the video that is playing.
 *
 * Legacy story renders record each chapter's offset (`start_s`) into the full
 * EN video. Everything else is a plain concat of its scenes, so a scene starts
 * where the previous ones' running times add up to; that sum is scaled to the
 * real length of the video once it is known (the zh cut runs to its own
 * timings, and the final encode can pad a scene by a frame or two).
 */
export function chaptersFor(e: Edition, lang: Lang, videoDuration: number | null): Chapter[] {
  const clips = e.clips ?? []
  if (clips.length === 0) return []
  const src = videoSrc(e, lang)
  const recorded = clips.every((c) => typeof c.start_s === 'number')

  if (recorded && lang === 'en' && clips.every((c) => c.file === undefined || c.file === src)) {
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
